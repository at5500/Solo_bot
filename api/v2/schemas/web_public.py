from typing import Annotated

from pydantic import BaseModel, Field


class AccountSummaryResponse(BaseModel):
    identity_id: str
    email: str | None = None
    tg_id: int | None = None
    linked_telegram: bool = False
    created_at: str | None = None
    password_set: bool = False
    referral_code: str = ""
    balance: float = 0.0
    trial_status: int = 0
    keys_total: int = 0
    referrals_total: int = 0
    referrals_active: int = 0
    referral_bonus_total: float = 0.0
    gifts_sent: int = 0
    gifts_claimed: int = 0
    coupons_used: int = 0
    partner_enabled: bool = False
    partner_code: str = ""
    partner_balance: float = 0.0
    partner_percent: float = 0.0
    partner_percent_custom: bool = False
    partner_referred_total: int = 0
    partner_referred_paid: int = 0
    partner_payout_method: str | None = None
    partner_payout_destination: str | None = None
    partner_last_payout: "PartnerPayoutEntryResponse | None" = None
    unread_notifications: int = 0


class AccountKeyActionsAvailability(BaseModel):
    can_connect_device: bool = False
    can_connect_router: bool = False
    can_connect_tv: bool = False
    can_renew: bool = False
    can_addons: bool = False
    can_reset_hwid: bool = False
    can_qr: bool = False
    can_delete: bool = False
    can_change_location: bool = False


class AccountKeyDetailsResponse(BaseModel):
    client_id: str
    email: str
    alias: str | None = None
    expiry_time: int = 0
    is_frozen: bool = False
    tariff_name: str = ""
    subgroup_title: str = ""
    traffic_limit_gb: int = 0
    used_traffic_gb: float | None = None
    device_limit: int = 0
    connected_devices: int = 0
    is_tariff_configurable: bool = False
    addons_devices_enabled: bool = False
    addons_traffic_enabled: bool = False
    can_renew: bool = True


class AccountKeyResponse(BaseModel):
    email: str
    alias: str | None = None
    client_id: str
    tariff_id: int | None = None
    server_id: str
    created_at: int = 0
    expiry_time: int = 0
    key: str | None = None
    remnawave_link: str | None = None
    is_frozen: bool = False
    is_trial: bool = False
    actions: AccountKeyActionsAvailability | None = None


class AccountKeyAliasUpdateRequest(BaseModel):
    alias: str = Field(..., min_length=1, max_length=10)


class AccountKeyActionResponse(BaseModel):
    ok: bool = True
    message: str = ""


class AccountKeyRenewRequest(BaseModel):
    tariff_id: int | None = None
    provider_id: str | None = None
    success_url: str | None = None
    failure_url: str | None = None
    coupon_code: str | None = None
    selected_device_limit: int | None = None
    selected_traffic_limit: int | None = None


class AccountKeyRenewResponse(AccountKeyActionResponse):
    client_id: str
    tariff_id: int
    charged_rub: int = 0
    balance_rub: float = 0.0
    base_price_rub: int = 0
    discount_rub: int = 0
    final_price_rub: int = 0
    applied_coupon_code: str | None = None
    payment_required: bool = False
    required_amount_rub: int = 0
    payment_id: str | None = None
    payment_url: str | None = None
    requires_tariff_selection: bool = False
    available_tariff_group: str | None = None
    is_switch: bool = False
    credit_to_balance_rub: int = 0
    refund_to_balance_rub: int = 0
    credit_days: int = 0
    credit_value_rub: int = 0
    new_device_limit: int | None = None
    new_traffic_gb: int | None = None


class AccountKeyResetHwidResponse(AccountKeyActionResponse):
    total_devices: int = 0
    reset_devices: int = 0


class KeyDeviceItem(BaseModel):
    hwid: str
    device_model: str | None = None
    platform: str | None = None
    os_version: str | None = None
    user_agent: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class KeyDevicesResponse(BaseModel):
    client_id: str
    items: list[KeyDeviceItem] = []
    device_limit: int | None = None


class KeyDeviceDeleteResponse(AccountKeyActionResponse):
    client_id: str
    hwid: str


class DeviceCooldownResponse(BaseModel):
    cooldown_minutes: int
    can_delete: bool
    remaining_minutes: int = 0


class TvConnectRequest(BaseModel):
    """Body for pushing a key's subscription to a TV via the Happ web-import code.

    Attributes:
        code: The short pairing code shown on the TV's Happ "Web import" screen.
    """

    code: str


class TvConnectResponse(AccountKeyActionResponse):
    """Result of a TV web-import push (inherits ``ok`` and ``message``)."""

    client_id: str


class AccountKeyQrResponse(AccountKeyActionResponse):
    link: str = ""
    image_data_url: str = ""


class AccountKeyLocationOptionResponse(BaseModel):
    server_name: str


class AccountKeyLocationsResponse(BaseModel):
    client_id: str
    current_server: str = ""
    locations: list[AccountKeyLocationOptionResponse] = []


class AccountKeyChangeLocationRequest(BaseModel):
    server_name: str = Field(..., min_length=1)


class AccountKeyChangeLocationResponse(AccountKeyActionResponse):
    client_id: str
    server_id: str = ""
    link: str = ""
    remnawave_link: str | None = None


class AccountKeyAddonOptionResponse(BaseModel):
    value: int
    label: str = ""


class AccountKeyAddonsPreviewRequest(BaseModel):
    selected_device_limit: int | None = None
    selected_traffic_gb: int | None = None
    include_device: bool | None = None
    include_traffic: bool | None = None
    provider_id: str | None = None
    success_url: str | None = None
    failure_url: str | None = None
    coupon_code: str | None = None


class AccountKeyAddonsPreviewResponse(BaseModel):
    client_id: str
    tariff_id: int
    addons_mode: str = ""
    has_device_option: bool = False
    has_traffic_option: bool = False
    current_device_limit: int | None = None
    current_traffic_gb: int | None = None
    selected_device_limit: int | None = None
    selected_traffic_gb: int | None = None
    device_options: list[AccountKeyAddonOptionResponse] = []
    traffic_options: list[AccountKeyAddonOptionResponse] = []
    total_price_rub: int = 0
    extra_price_rub: int = 0
    discount_rub: int = 0
    final_price_rub: int = 0
    applied_coupon_code: str | None = None
    balance_rub: float = 0.0


class AccountKeyApplyAddonsResponse(AccountKeyActionResponse):
    client_id: str
    tariff_id: int
    total_price_rub: int = 0
    extra_price_rub: int = 0
    discount_rub: int = 0
    final_price_rub: int = 0
    applied_coupon_code: str | None = None
    charged_rub: int = 0
    balance_rub: float = 0.0
    payment_required: bool = False
    required_amount_rub: int = 0
    payment_id: str | None = None
    payment_url: str | None = None


class AccountKeyActionsConfigResponse(BaseModel):
    renew_enabled: bool = True
    delete_enabled: bool = False
    qr_enabled: bool = False
    hwid_reset_enabled: bool = False
    country_change_enabled: bool = False
    instructions_enabled: bool = False
    addons_enabled: bool = False
    addons_mode: str = ""
    tv_connect_enabled: bool = False


class AccountKeyConnectionResponse(BaseModel):
    client_id: str
    online: bool = False
    is_frozen: bool = False
    expiry_time: int = 0
    expires_in_days: int = 0
    server_name: str = ""
    cluster_name: str = ""
    panel_type: str = ""
    protocol: str = ""
    is_online: bool | None = None
    online_at: str | None = None
    connected_devices: int | None = None


class AccountSearchHit(BaseModel):
    kind: str
    label: str
    sublabel: str = ""
    href: str = ""
    meta: str = ""


class AccountSearchResponse(BaseModel):
    query: str
    hits: list[AccountSearchHit] = []
    total: int = 0


class TariffConfigPriceResponse(BaseModel):
    price_rub: int


class TariffPurchaseRequest(BaseModel):
    tariff_id: int = Field(..., ge=1)
    selected_device_limit: int | None = None
    selected_traffic_gb: int | None = None
    provider_id: str | None = None
    success_url: str | None = None
    failure_url: str | None = None
    coupon_code: str | None = None


class TariffPurchaseResponse(BaseModel):
    ok: bool = True
    message: str = ""
    key_email: str | None = None
    charged_rub: int | None = None
    base_price_rub: int = 0
    discount_rub: int = 0
    final_price_rub: int = 0
    applied_coupon_code: str | None = None
    payment_required: bool = False
    required_amount_rub: int = 0
    payment_id: str | None = None
    payment_url: str | None = None
    # Set on the email-purchase flow when a blocking subscription already exists:
    # "active" (live paid key) or "frozen" (paid key on pause). None otherwise.
    subscription_state: str | None = None


# Same format check register applies (api/v2/schemas/identities.py) — defined
# locally to avoid an identities -> web_public import cycle.
_EMAIL_PATTERN = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
EmailStr = Annotated[str, Field(min_length=3, max_length=254, pattern=_EMAIL_PATTERN)]


class TariffPurchaseByEmailRequest(TariffPurchaseRequest):
    """Guest purchase: the buyer identity is derived from ``email`` server-side
    instead of a session cookie. ``turnstile_token`` mirrors register and is
    only checked when Turnstile is enabled."""

    email: EmailStr
    turnstile_token: str | None = None


class GiftCreateRequest(BaseModel):
    tariff_id: int = Field(..., ge=1)
    selected_device_limit: int | None = None
    selected_traffic_gb: int | None = None
    provider_id: str | None = None
    success_url: str | None = None
    failure_url: str | None = None


class GiftCreatePreviewResponse(BaseModel):
    ok: bool = True
    price_rub: int = 0
    balance_rub: float = 0.0
    sufficient_funds: bool = True
    tariff_name: str = ""
    duration_days: int = 0


class GiftCreateResponse(BaseModel):
    ok: bool = True
    message: str = ""
    gift_id: str = ""
    site_gift_link: str = ""
    tariff_name: str = ""
    duration_days: int = 0
    price_charged: int = 0
    balance_rub: float = 0.0
    payment_required: bool = False
    required_amount_rub: int = 0
    payment_id: str | None = None
    payment_url: str | None = None


class GiftUsageEntry(BaseModel):
    user_id: int
    used_at: str | None = None


class MyGiftItem(BaseModel):
    gift_id: str
    tariff_name: str = ""
    duration_days: int = 0
    price_rub: int = 0
    created_at: str | None = None
    expiry_time: str | None = None
    is_used: bool = False
    is_unlimited: bool = False
    max_usages: int | None = None
    site_gift_link: str = ""
    usages: list[GiftUsageEntry] = []


class MyGiftsResponse(BaseModel):
    ok: bool = True
    gifts: list[MyGiftItem] = []
    total: int = 0
    limit: int = 20
    offset: int = 0


class GiftRedeemRequest(BaseModel):
    gift_code: str = Field(..., min_length=1)


class GiftRedeemResponse(BaseModel):
    ok: bool = True
    message: str = ""
    gift_id: str = ""
    tariff_id: int = 0
    duration_days: int = 0


class ReferralApplyRequest(BaseModel):
    referrer_code: str | None = Field(None, min_length=1)
    referrer_tg_id: int | None = Field(None, ge=1)


class ReferralApplyResponse(BaseModel):
    ok: bool = True
    message: str = ""
    referrer_code: str = ""
    referrer_user_id: int = 0
    referrer_tg_id: int | None = None
    referred_user_id: int = 0
    referred_tg_id: int | None = None


class ReferralTopEntryResponse(BaseModel):
    position: int
    referrer_user_id: int
    referrals_count: int
    display_id: str


class ReferralTopResponse(BaseModel):
    user_referrals_count: int = 0
    user_position: int | None = None
    top: list[ReferralTopEntryResponse] = []


class ReferralListEntry(BaseModel):
    referred_user_id: int
    referred_tg_id: int | None = None
    display_id: str = ""
    reward_issued: bool = False


class ReferralListResponse(BaseModel):
    total: int = 0
    items: list[ReferralListEntry] = []


class ReferralQrResponse(BaseModel):
    ok: bool = True
    link: str = ""
    image_data_url: str = ""


class GiftQrResponse(BaseModel):
    ok: bool = True
    link: str = ""
    image_data_url: str = ""


class ReferralConditionsResponse(BaseModel):
    title: str = ""
    summary: str = ""
    bonus_mode: str = ""
    bonus_mode_label: str = ""
    level_lines: list[str] = []
    rules: list[str] = []


class PartnerConditionsResponse(BaseModel):
    title: str = ""
    summary: str = ""
    bonus_mode: str = ""
    bonus_mode_label: str = ""
    level_lines: list[str] = []
    rules: list[str] = []
    examples: list[str] = []
    min_payout_rub: float = 0.0
    payout_methods: list[str] = []
    custom_amount_enabled: bool = False


class PartnerQrResponse(BaseModel):
    ok: bool = True
    link: str = ""
    image_data_url: str = ""


class PartnerApplyRequest(BaseModel):
    partner_code: str | None = Field(None, min_length=1)
    partner_tg_id: int | None = Field(None, ge=1)


class PartnerApplyResponse(BaseModel):
    ok: bool = True
    message: str = ""
    partner_code: str = ""
    # Internal sequential user.id columns (partner_user_id /
    # joined_user_id) were stripped to avoid leaking row counts and
    # registration order — audit F-015.
    partner_tg_id: int | None = None
    joined_tg_id: int | None = None


class PartnerTopEntryResponse(BaseModel):
    position: int
    partner_user_id: int
    referred_count: int
    display_id: str


class PartnerTopResponse(BaseModel):
    user_referred_count: int = 0
    user_position: int | None = None
    top: list[PartnerTopEntryResponse] = []


class PartnerInvitedEntry(BaseModel):
    tg_id: int
    joined_at: str | None = None
    balance: float = 0.0
    keys_count: int = 0
    payments_count: int = 0


class PartnerInvitedResponse(BaseModel):
    total: int = 0
    items: list[PartnerInvitedEntry] = []


class CouponApplyRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=128)


class CouponApplyResponse(BaseModel):
    ok: bool = True
    message: str = ""
    coupon_code: str = ""
    amount: int = 0
    balance: float = 0.0


class PartnerPayoutToBalanceRequest(BaseModel):
    amount_rub: float = Field(..., gt=0)


class PartnerPayoutToBalanceResponse(BaseModel):
    ok: bool = True
    message: str = ""
    amount_rub: float = 0.0
    partner_balance: float = 0.0
    balance: float = 0.0


class PartnerPayoutRequestCreate(BaseModel):
    amount_rub: float = Field(..., gt=0)


class PartnerPayoutRequestResponse(BaseModel):
    ok: bool = True
    message: str = ""
    request_id: int | None = None
    amount_rub: float = 0.0
    status: str = "pending"
    balance_rub: float = 0.0


class PartnerPayoutEntryResponse(BaseModel):
    id: int
    amount_rub: float = 0.0
    status: str = ""
    created_at: str | None = None
    method: str | None = None
    destination: str | None = None


class PartnerPayoutHistoryResponse(BaseModel):
    total: int = 0
    items: list[PartnerPayoutEntryResponse] = []


class PartnerPayoutMethodUpdateRequest(BaseModel):
    method: str = Field(..., min_length=1, max_length=16, description="Машинный код способа: card|sbp|usdt|ton")
    destination: str = Field(..., min_length=1, max_length=128, description="Реквизиты выплаты — номер карты, телефон СБП или адрес кошелька")


class PartnerPayoutMethodResponse(BaseModel):
    ok: bool = True
    method: str = ""
    destination: str = ""


class PartnerCodeUpdateRequest(BaseModel):
    code: str = Field(..., min_length=3, max_length=32, description="New partner code (a-z, 0-9, _)")


class PartnerCodeResponse(BaseModel):
    ok: bool = True
    code: str = ""


class PartnerPayoutMethodOption(BaseModel):
    key: str
    label: str
    hint: str = ""


class PartnerPayoutMethodState(BaseModel):
    configured: bool = False
    method: str | None = None
    method_label: str | None = None
    masked: str | None = None
    methods: list[PartnerPayoutMethodOption] = []


class PartnerPayoutMethodUpdate(BaseModel):
    method: str = Field(..., min_length=1, max_length=20)
    requisites: str = Field(..., min_length=1, max_length=128)


# AccountSummaryResponse references PartnerPayoutEntryResponse, which is
# declared later in this module — resolve the forward reference now.
AccountSummaryResponse.model_rebuild()
