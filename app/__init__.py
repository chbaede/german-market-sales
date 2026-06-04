from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask

from .config import Config
from .routes import bp
from .services.translations import translate_text


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    config_object.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    register_template_filters(app)
    app.register_blueprint(bp)
    return app


def register_template_filters(app: Flask) -> None:
    @app.template_filter("eur")
    def format_eur(value: float | int | None) -> str:
        if value is None:
            return "-"
        return f"{value:,.2f} EUR".replace(",", "X").replace(".", ",").replace("X", ".")

    @app.template_filter("pct")
    def format_percent(value: float | int | None) -> str:
        if value is None or value <= 0:
            return ""
        return f"-{round(value)}%"

    @app.template_filter("amount")
    def format_amount(value: float | int | None) -> str:
        if value is None:
            return ""
        if float(value).is_integer():
            return str(int(value))
        return str(value).replace(".", ",")

    @app.template_filter("date_de")
    def format_date(value: str | None) -> str:
        if not value:
            return "-"
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            date_part = parsed.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
        except ValueError:
            date_part = value.split("T", 1)[0]
        parts = date_part.split("-")
        if len(parts) != 3:
            return value
        return f"{parts[2]}.{parts[1]}.{parts[0]}"

    @app.template_filter("ko_text")
    def ko_text(value: str | None) -> str:
        return translate_text(value)
