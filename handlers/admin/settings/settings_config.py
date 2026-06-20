from typing import Final


BUTTON_TITLES: Final[dict[str, str]] = {
    "CHANNEL_BUTTON_ENABLE": "Канал",
    "DONATIONS_BUTTON_ENABLE": "Донаты",
    "BALANCE_BUTTON_ENABLE": "Баланс",
    "REFERRAL_QR_BUTTON_ENABLE": "QR реф.меню",
    "DELETE_KEY_BUTTON_ENABLE": "Удалить подп-ку",
    "INSTRUCTIONS_BUTTON_ENABLE": "Инструкции",
    "GIFT_BUTTON_ENABLE": "Подарки",
    "REFERRAL_BUTTON_ENABLE": "Реф.система",
    "TOP_REFERRAL_BUTTON_ENABLE": "Топ-5 рефералов",
    "QRCODE_BUTTON_ENABLE": "QR подписки",
    "HWID_RESET_BUTTON_ENABLE": "Сброс HWID",
    "ANDROID_TV_BUTTON_ENABLE": "Android TV",
    "COUPON_BUTTON_ENABLE": "Активировать купон",
}

NOTIFICATION_TITLES: Final[dict[str, str]] = {
    "RENEW_ENABLED": "Авто-продление",
    "EXPIRY_24H_ENABLED": "За 24 часа",
    "EXPIRY_10H_ENABLED": "За 10 часов",
    "DELETE_KEY_ENABLED": "Удалять просроченные",
    "RENEW_EXPIRED_ENABLED": "Продлевать просроченные",
    "HOT_LEADS_ENABLED": "Горячие лиды",
    "COLD_LEADS_ENABLED": "Холодные лиды",
    "RETURNING_ENABLED": "Возврат давно ушедших",
}

NOTIFICATION_TIME_FIELDS: Final[dict[str, str]] = {
    "BASE_NOTIFICATION_MINUTE": "Проверка (сек)",
    "INACTIVE_USER_ENABLED": "Неактивные (ч)",
    "EXPIRY_24H_BEFORE_HOURS": "До 24ч (ч)",
    "EXPIRY_10H_BEFORE_HOURS": "До 10ч (ч)",
    "DELETE_KEY_DELAY_MINUTES": "Удаление (мин)",
    "EXTRA_DAYS_AFTER_EXPIRY": "Дни к пробнику",
    "INACTIVE_TRAFFIC_ENABLED": "Трафик неакт. (ч)",
    "HOT_LEADS_INTERVAL_HOURS": "Гор.лиды (ч)",
    "COLD_LEADS_INTERVAL_HOURS": "Хол.лиды (ч)",
    "RETURNING_MIN_DAYS": "Давно ушли от (дн)",
    "RETURNING_MAX_DAYS": "Давно ушли до (дн)",
    "DISCOUNT_ACTIVE_HOURS": "Скидка (ч)",
    "RENEW_BUTTON_BEFORE_DAYS": "Кнопка продл. за (дн)",
}

PAYMENT_PROVIDER_TITLES: Final[dict[str, str]] = {
    "YOOKASSA": "YooKassa",
    "YOOMONEY": "YooMoney",
    "ROBOKASSA": "Robokassa",
    "KASSAI_CARDS": "KassaAI карты",
    "KASSAI_SBP": "KassaAI СБП",
    "WATA_RU": "WATA карты РФ / СБП",
    "WATA_INT": "WATA международные",
    "PARITYPAY_SBP": "ParityPay СБП",
    "PLATEGA_SBP": "Platega СБП",
    "PLATEGA_CARDS": "Platega карты РФ",
    "PLATEGA_INT": "Platega международные",
    "PLATEGA_CRYPTO": "Platega крипто",
    "TRIBUTE": "Tribute",
    "HELEKET": "Heleket",
    "CRYPTOBOT": "CryptoBot",
    "FREEKASSA": "FreeKassa",
    "STARS": "Telegram Stars",
}

MODES_TITLES: Final[dict[str, str]] = {
    "CAPTCHA_ENABLED": "Капча",
    "CHANNEL_CHECK_ENABLED": "Обязат. канал",
    "SHOW_START_MENU_ONLY_ONCE": "Старт один раз",
    "INLINE_MODE_ENABLED": "Инлайн-режим",
    "RANDOM_SUBSCRIPTIONS_ENABLED": "Случайные страны",
    "COUNTRY_SELECTION_ENABLED": "Режим стран",
    "REMNAWAVE_WEBAPP_ENABLED": "Remna WebApp",
    "REMNAWAVE_WEBAPP_OPEN_IN_BROWSER": "WebApp в браузере",
    "HAPP_CRYPTOLINK_ENABLED": "Happ-ссылки",
    "LEGACY_LINKS_ENABLED": "Старые ссылки",
    "DIRECT_START_DISABLED": "Тихий режим",
    "TRIAL_TIME_DISABLED": "Отключить триал",
    "SUPPORT_TRIAGE_ENABLED": "Опросник поддержки",
    "PROTECT_CONTENT_ENABLED": "Защита контента",
    "TARIFF_OPTIONS_PAGINATION": "Слайдер опций",
}

MONEY_FIELDS: Final[dict[str, str]] = {
    "FX_MARKUP": "Наценка FX (%)",
    "RUB_TO_USD": "Курс USD/RUB",
    "CASHBACK": "Кэшбэк (%)",
}
