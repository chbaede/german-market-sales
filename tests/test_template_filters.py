from app import create_app


def test_date_filter_uses_german_local_date():
    app = create_app()

    assert app.jinja_env.filters["date_de"]("2026-05-25T22:00:00Z") == "26.05.2026"
