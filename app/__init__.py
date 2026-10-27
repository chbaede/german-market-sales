from flask import Flask

from .config import Config
from .routes import bp
from .services.formatting import (
    format_amount_value,
    format_date_de,
    format_eur_display,
    format_percent_display,
)
from .services.translations import translate_text


def create_app(config_object: type[Config] = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    config_object.CACHE_DIR.mkdir(parents=True, exist_ok=True)

    register_template_filters(app)
    app.register_blueprint(bp)

    if app.config.get("AUTO_REFRESH_ENABLED", True) and not app.config.get("TESTING", False):
        from .services.scheduler import AutoRefreshScheduler

        scheduler = AutoRefreshScheduler(
            app=app,
            check_interval_seconds=int(app.config.get("SCHEDULER_CHECK_INTERVAL_SECONDS", 900)),
            max_cache_age_hours=int(app.config.get("SCHEDULER_MAX_CACHE_AGE_HOURS", 168)),
        )
        scheduler.start()
        app.extensions["auto_scheduler"] = scheduler

    return app


def register_template_filters(app: Flask) -> None:
    app.add_template_filter(format_eur_display, name="eur")
    app.add_template_filter(format_percent_display, name="pct")
    app.add_template_filter(format_amount_value, name="amount")
    app.add_template_filter(format_date_de, name="date_de")
    app.add_template_filter(translate_text, name="ko_text")
