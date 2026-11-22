"""Спільний хелпер тестів: увійти в дашборд із потрібною роллю без форми входу (сесія створюється напряму)."""
from dronewatch.service.dashboard_auth import SessionStore
from dronewatch.service.dashboard_web import COOKIE, DashboardAccess


def login_as(client, role, user="alice") -> None:
    """Кладе в cookie клієнта сесію `user` з роллю `role`. Якщо в застосунку вхід вимкнено (DashboardAccess() без сесій),
    вмикає сховище сесій: інакше анонім лишається viewer і дії operator/admin недоступні (так і має бути)."""
    access = getattr(client.app.state, "access", None)
    if access is None or access.sessions is None:
        access = DashboardAccess(access.users if access else None, SessionStore(3600), access.limiter if access else None)
        client.app.state.access = access
    client.cookies.set(COOKIE, access.sessions.create(user, role))
