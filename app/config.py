from decimal import Decimal
from functools import cached_property

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')

    bot_token: str = ''
    admin_ids: str = ''
    database_url: str = 'sqlite+aiosqlite:///./bot.db'
    provider_name: str = 'grizzly'
    provider_api_key: str = ''
    textverified_api_username: str = ''
    textverified_api_key: str = ''
    smspool_api_key: str = ''
    textverified_webhook_secret: str = ''
    live_purchases_enabled: bool = False
    wallet_currency: str = 'USD'
    provider_price_to_wallet: Decimal = Decimal('0')
    payment_provider: str = 'cryptobot'
    payment_api_key: str = ''
    nowpayments_api_key: str = ''
    nowpayments_ipn_secret: str = ''
    nowpayments_api_base: str = 'https://api.nowpayments.io/v1'
    public_base_url: str = ''
    payments_enabled: bool = False
    payment_test_admin_only: bool = True
    payment_base_url: str = 'https://pay.crypt.bot/api'
    markup_percent: Decimal = Decimal('50')
    min_topup: Decimal = Decimal('5')
    max_topup: Decimal = Decimal('500')
    activation_timeout_seconds: int = 900
    provider_low_balance_alert: Decimal = Decimal('10')
    allowed_services: str = 'telegram,whatsapp,google,facebook,instagram,discord,amazon,microsoft'
    allowed_countries: str = ''
    blocked_services: str = 'bank,government,cryptoexchange'

    @cached_property
    def admins(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_ids.split(',') if x.strip().isdigit()}

    def service_allowed(self, service: str) -> bool:
        service = service.lower()
        allowed = {x.strip().lower() for x in self.allowed_services.split(',') if x.strip()}
        blocked = {x.strip().lower() for x in self.blocked_services.split(',') if x.strip()}
        return service not in blocked and (not allowed or service in allowed)

    def country_allowed(self, country: str) -> bool:
        allowed = {x.strip().lower() for x in self.allowed_countries.split(',') if x.strip()}
        return not allowed or country.lower() in allowed

    @property
    def provider_credentials_ready(self) -> bool:
        if self.provider_name == 'textverified':
            return bool(self.textverified_api_username and self.textverified_api_key)
        if self.provider_name == 'smspool':
            return bool(self.smspool_api_key)
        return bool(self.provider_api_key)

    @property
    def live_ready(self) -> bool:
        # Both providers quote USD prices. Conversion must be exactly 1.
        if self.provider_name in ('textverified', 'smspool'):
            return bool(self.live_purchases_enabled and self.provider_credentials_ready
                        and self.wallet_currency == 'USD'
                        and self.provider_price_to_wallet == Decimal('1'))
        return bool(self.live_purchases_enabled and self.provider_credentials_ready
                    and self.provider_price_to_wallet > 0)

    @property
    def payment_ready(self) -> bool:
        if self.payment_provider == 'nowpayments':
            return bool(self.payments_enabled and self.nowpayments_api_key
                        and self.nowpayments_ipn_secret and self.public_base_url.startswith('https://'))
        return bool(self.payments_enabled and self.payment_api_key)


settings = Config()
