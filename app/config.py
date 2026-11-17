from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Muhit o'zgaruvchilaridan (yoki .env fayldan) o'qiladigan sozlamalar.

    Mehmonxonaning biznes sozlamalari (kirish oynasi, Wi-Fi paroli, chegirma
    chegarasi va h.k.) bu yerda emas, bazadagi `settings` jadvalida turadi va
    veb-paneldan o'zgartiriladi.
    """

    model_config = SettingsConfigDict(env_file=".env", env_prefix="SQ_", extra="ignore")

    database_url: str = "sqlite:///./shaffof.db"
    base_url: str = "http://localhost:8000"  # mehmon havolalari va QR shu manzilga
    secret_key: str = "change-me-please-32-bytes-minimum!!"
    timezone: str = "Asia/Tashkent"
    storage_dir: str = "./storage"

    # Telegram
    telegram_bot_token: str = ""
    telegram_api_base: str = "https://api.telegram.org"

    # SMS (eskiz.uz yoki console)
    sms_provider: str = "console"
    sms_eskiz_email: str = ""
    sms_eskiz_password: str = ""
    sms_eskiz_from: str = "4546"

    # Edge agent (mehmonxonadagi NVR yonidagi kichik kompyuter)
    edge_secret: str = "change-me-edge-secret"

    # Ilk ega hisobi (faqat bo'sh bazada `python -m app.cli init` uchun)
    debug: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
