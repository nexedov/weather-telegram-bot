import asyncio
import logging
import sys
import os
import time
from dotenv import load_dotenv
import httpx
from aiogram import Bot, Dispatcher, html, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery, TelegramObject
from aiogram.exceptions import TelegramBadRequest

# ЗАГРУЗКА И ВАЛИДАЦИЯ НАСТРОЕК
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if not BOT_TOKEN or not WEATHER_API_KEY:
    print("\n[КРИТИЧЕСКАЯ ОШИБКА] Проверьте настройки в файле .env!")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, stream=sys.stdout)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# КЭШИРОВАНИЕ
class WeatherCache:
    def __init__(self, ttl_seconds: int = 600):
        self.storage = {}
        self.ttl = ttl_seconds

    def get(self, key: str) -> dict | None:
        if key in self.storage:
            cache_data, timestamp = self.storage[key]
            if time.time() - timestamp < self.ttl:
                print(f"[CACHE HITTED] Данные для '{key}' взяты из памяти.")
                return cache_data
            else:
                del self.storage[key]
        return None

    def set(self, key: str, data: dict):
        self.storage[key] = (data, time.time())


weather_cache = WeatherCache()
forecast_cache = WeatherCache()


# 3. АНТИФЛУД
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.5):
        self.limit = limit
        self.users = {}
        super().__init__()

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user_id = None
        is_callback = False

        if isinstance(event, Message) and event.from_user:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user:
            user_id = event.from_user.id
            is_callback = True

        if user_id:
            now = time.time()
            if user_id in self.users:
                last_time = self.users[user_id]
                if now - last_time < self.limit:
                    if is_callback:
                        await event.answer("⚠️ Слишком частые клики!", show_alert=False)
                    else:
                        await event.answer("⚠️ Пожалуйста, подождите между запросами.")
                    return
            self.users[user_id] = now
        return await handler(event, data)


dp.message.middleware(ThrottlingMiddleware(limit=1.5))
dp.callback_query.middleware(ThrottlingMiddleware(limit=1.0))


# 4. ОФОРМЛЕНИЕ ИНФОРМЕРОВ И Т.Д
def get_weather_emoji(weather_id: int) -> str:
    if 200 <= weather_id <= 232:
        return "🌩"
    elif 300 <= weather_id <= 321:
        return "🌦"
    elif 500 <= weather_id <= 531:
        return "🌧"
    elif 600 <= weather_id <= 622:
        return "❄️"
    elif 701 <= weather_id <= 781:
        return "🌫"
    elif weather_id == 800:
        return "☀️"
    elif 801 <= weather_id <= 804:
        return "☁️"
    return "🌤"


def get_clothing_advice(temp: float, wind_speed: float, status: str) -> str:
    advice = []
    if temp < -10:
        advice.append(
            "🥶 На улице лютый мороз! Рекомендуется надевать самую теплую зимнюю куртку, термобелье, перчатки, шарф и теплую шапку.")
    elif -10 <= temp < 0:
        advice.append("❄️ Минусовая температура. Отличным выбором станет зимняя куртка, шапка и шарф обязательны.")
    elif 0 <= temp < 10:
        advice.append("🧥 На улице довольно холодно. Рекомендуется надеть демисезонную куртку или легкий пуховик.")
    elif 10 <= temp < 18:
        advice.append("🍂 Прохладная погода. Будет максимально комфортно в ветровке, худи или кожаной куртке.")
    elif 18 <= temp < 25:
        advice.append("👕 Теплая и приятная погода. Можно смело выходить в легкой футболке, рубашке или свитшоте.")
    else:
        advice.append("☀️ На улице жара! Доставайте шорты и футболки. Не забывайте про головной убор.")

    if wind_speed > 7.0:
        advice.append("💨 Дует сильный порывистый ветер! Выбирайте непродуваемую куртку-ветровку.")
    if status in ["Rain", "Drizzle", "Thunderstorm"]:
        advice.append("☔️ Ожидаются осадки. Обязательно захватите с собой зонт!")
    elif status == "Snow":
        advice.append("🌨 Идет плотный снег. Выбирайте обувь с нескользящей подошвой.")
    return "\n\n".join(advice)


location_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="📍 Поделиться геолокацией", request_location=True)]],
    resize_keyboard=True
)


def get_main_inline_keyboard(lat: float, lon: float) -> InlineKeyboardMarkup:
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Прогноз на 24ч", callback_data=f"fc_{lat}_{lon}"),
            InlineKeyboardButton(text="👔 Что надеть?", callback_data=f"cl_{lat}_{lon}")
        ]
    ])


def get_back_inline_keyboard(lat: float, lon: float) -> InlineKeyboardMarkup:
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад к текущей погоде", callback_data=f"back_{lat}_{lon}")]
    ])


# 5. ОСНОВНЫЕ ХЭНДЛЕРЫ ЗАПРОСОВ
@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    await message.answer(
        f"Привет, {html.bold(message.from_user.full_name)}! 🌤\n"
        f"Напишите название города или отправьте геолокацию с помощью кнопки ниже.",
        reply_markup=location_keyboard
    )


@dp.message(F.location)
async def location_handler(message: Message) -> None:
    waiting_message = await message.answer("Определяю координаты...")
    lat, lon = message.location.latitude, message.location.longitude
    await process_weather_request(message, {"lat": lat, "lon": lon}, waiting_message)


@dp.message(F.text)
async def text_handler(message: Message) -> None:
    city_name = html.quote(message.text.strip())
    if len(city_name) > 50:
        await message.answer("❌ Слишком длинное название города.")
        return
    waiting_message = await message.answer("Запрашиваю данные...")
    await process_weather_request(message, {"q": city_name}, waiting_message)


# 6. ОСНОВАЯ СИСТЕМА ПОГОДЫ
async def fetch_weather_data(query_params: dict) -> tuple:
    cache_key = f"{query_params.get('q', '')}_{query_params.get('lat', '')}_{query_params.get('lon', '')}".lower()
    cached_result = weather_cache.get(cache_key)
    if cached_result:
        return cached_result

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {**query_params, "appid": WEATHER_API_KEY.strip(), "units": "metric", "lang": "ru"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        lat, lon = data["coord"]["lat"], data["coord"]["lon"]
        city = data["name"]
        temp = round(float(data["main"]["temp"]))
        feels_like = round(float(data["main"]["feels_like"]))
        humidity = data["main"]["humidity"]
        description = data["weather"][0]["description"].capitalize()
        wind_speed = float(data["wind"]["speed"])
        emoji = get_weather_emoji(data["weather"][0]["id"])

        text = (
            f"{emoji} Погода в: {html.bold(city)}\n"
            f"⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            f"🌡 Температура: {html.bold(f'{temp}°C')}\n"
            f"🤔 Ощущается как: {feels_like}°C\n"
            f"💧 Влажность: {humidity}%\n"
            f"💨 Ветер: {wind_speed} м/с\n"
            f"📝 На улице: {description}"
        )

        result = (text, lat, lon)
        weather_cache.set(cache_key, result)
        return result


async def process_weather_request(message: Message, query_params: dict, waiting_message: Message) -> None:
    try:
        text, lat, lon = await fetch_weather_data(query_params)
        try:
            await waiting_message.delete()
        except TelegramBadRequest:
            pass
        await message.answer(text, reply_markup=get_main_inline_keyboard(lat, lon))
    except httpx.HTTPStatusError as e:
        try:
            await waiting_message.delete()
        except TelegramBadRequest:
            pass
        if e.response.status_code == 404:
            await message.answer("❌ Локация не найдена.", reply_markup=location_keyboard)
        else:
            await message.answer(f"⚠️ Ошибка сервера: {e.response.status_code}", reply_markup=location_keyboard)
    except Exception as e:
        try:
            await waiting_message.delete()
        except TelegramBadRequest:
            pass
        print(f"[ERROR]: {e}")
        await message.answer("⚠️ Не удалось получить данные погоды.", reply_markup=location_keyboard)


# 7. НАЖАТИЕ, ИЗМЕНЕНИЕ, НАСТРОЙКА ВСЕХ КНОПОК
@dp.callback_query(F.data.startswith("fc_") | F.data.startswith("cl_") | F.data.startswith("back_"))
async def callback_weather_details(callback: CallbackQuery) -> None:
    action, lat, lon = callback.data.split("_")

    if action == "back":
        try:
            text, _, _ = await fetch_weather_data({"lat": lat, "lon": lon})
            await callback.message.edit_text(text, reply_markup=get_main_inline_keyboard(lat, lon))
            await callback.answer()
        except Exception:
            await callback.answer("⚠️ Ошибка возврата.", show_alert=True)
        return

    url = "https://api.openweathermap.org/data/2.5/forecast"
    cache_key_fc = f"fc_{lat}_{lon}"
    cached_forecast = forecast_cache.get(cache_key_fc)

    if cached_forecast:
        data = cached_forecast
    else:
        params = {"lat": lat, "lon": lon, "appid": WEATHER_API_KEY.strip(), "units": "metric", "lang": "ru"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                forecast_cache.set(cache_key_fc, data)
        except Exception as e:
            print(f"[FETCH FORECAST ERROR]: {e}")
            await callback.answer("⚠️ Ошибка загрузки данных прогноза.", show_alert=True)
            return

    try:
        if action == "fc":
            forecast_lines = [f"📊 {html.bold('Прогноз на ближайшие 24 часа:')}\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯"]
            for item in data["list"][:8]:
                time_str = item["dt_txt"].split()[1][:-3]
                t = round(float(item["main"]["temp"]))
                desc = item["weather"][0]["description"]
                emo = get_weather_emoji(item["weather"][0]["id"])
                forecast_lines.append(f"⏰ {time_str}  ➔  {emo} {t}°C, {desc}")

            await callback.message.edit_text("\n".join(forecast_lines), reply_markup=get_back_inline_keyboard(lat, lon))
            await callback.answer()

        elif action == "cl":
            next_forecast = data["list"][0]
            t = round(float(next_forecast["main"]["temp"]))
            w = float(next_forecast["wind"]["speed"])
            st = next_forecast["weather"][0]["main"]

            advice_text = f"👔 {html.bold('Рекомендации по выбору одежды:')}\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
            advice_text += get_clothing_advice(t, w, st)
            await callback.message.edit_text(advice_text, reply_markup=get_back_inline_keyboard(lat, lon))
            await callback.answer()

    except Exception as e:
        print(f"\n[CALLBACK ERROR]: {e}\n")
        await callback.answer("⚠️ Не удалось отобразить информацию.", show_alert=True)


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
