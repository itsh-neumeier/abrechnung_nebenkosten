"""Login, Passwort-Reset, Benutzerverwaltung und Mieterportal (Router)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import auth, mailer
from .config import config
from .db import Billing, Party, User, get_session, get_settings, save_settings
from .render import invoice_html, invoice_pdf, party_result, pdf_name, period_text, templates

router = APIRouter()


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def current(request: Request) -> Optional[auth.CurrentUser]:
    return getattr(request.state, "user", None)


def require_admin(request: Request) -> None:
    u = current(request)
    if getattr(request.state, "auth_enabled", False) and (u is None or not u.is_admin):
        raise HTTPException(403, "Nur für Verwalter")


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


def _base_url(request: Request) -> str:
    return config.app_base_url or str(request.base_url).rstrip("/")


def _set_cookie(resp: Response, request: Request, value: str, days: float) -> None:
    resp.set_cookie(auth.COOKIE, value, max_age=int(days * 86400), httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


# --------------------------------------------------------------------------- Login / Logout / Ersteinrichtung
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return _page(request, "login.html", next=_safe_next(next), mail_ok=mailer.configured())


@router.post("/login")
async def login(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    login_name = str(form.get("username", "")).strip()
    nxt = _safe_next(str(form.get("next", "/")))
    ip = request.client.host if request.client else "?"
    keys = (f"ip:{ip}", f"user:{login_name.lower()}")
    if any(auth.login_throttle.blocked(k) for k in keys):
        return _redirect(f"/login?next={quote(nxt)}", "Zu viele Fehlversuche – bitte in einigen Minuten erneut versuchen.")
    u = auth.find_user(s, login_name)
    if u is None or not u.active or not auth.verify_password(str(form.get("password", "")), u.password_hash):
        for k in keys:
            auth.login_throttle.hit(k)
        return _redirect(f"/login?next={quote(nxt)}", "Benutzername oder Passwort falsch.")
    for k in keys:
        auth.login_throttle.clear(k)
    u.last_login = datetime.now()
    s.commit()
    days = 30 if form.get("remember") else 0.5
    if u.role != "admin" and not nxt.startswith(("/portal", "/account")):
        nxt = "/portal"
    resp = RedirectResponse(nxt, status_code=303)
    _set_cookie(resp, request, auth.make_cookie(s, u, days), days)
    return resp


@router.get("/logout")
def logout():
    resp = _redirect("/login", "Abgemeldet.")
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, s: Session = Depends(get_session)):
    if auth.has_users(s):
        return _redirect("/login")
    return _page(request, "setup.html")


@router.post("/setup")
async def setup(request: Request, s: Session = Depends(get_session)):
    if auth.has_users(s):
        return _redirect("/login")
    form = await request.form()
    pw = str(form.get("password", ""))
    if problem := auth.password_problem(pw):
        return _redirect("/setup", problem)
    if pw != str(form.get("password2", "")):
        return _redirect("/setup", "Passwörter stimmen nicht überein.")
    username = str(form.get("username", "")).strip() or "admin"
    u = User(username=username, name=str(form.get("name", "")).strip(), email=str(form.get("email", "")).strip(),
             role="admin", active=True)
    auth.set_password(u, pw)
    s.add(u)
    s.commit()
    resp = _redirect("/", f"Verwalter „{username}“ angelegt – Login ist jetzt aktiv.")
    _set_cookie(resp, request, auth.make_cookie(s, u, 0.5), 0.5)
    return resp


# --------------------------------------------------------------------------- Passwort vergessen / zurücksetzen
RESET_SENT = ("Falls ein Konto mit dieser Angabe und E-Mail-Adresse existiert, wurde ein Link zum Zurücksetzen "
              "verschickt (1 Stunde gültig).")


def send_reset_mail(request: Request, s: Session, u: User, invite: bool = False) -> None:
    hours = 24 * 7 if invite else 1
    token = auth.create_reset(s, u, hours=hours)
    link = f"{_base_url(request)}/password/reset?token={token}"
    st = get_settings(s)
    sender = st["landlord_name"] or "Ihre Hausverwaltung"
    if invite:
        subject = "Zugang zur Nebenkostenabrechnung"
        body = (f"Hallo {u.name or u.username},\n\nfür Sie wurde ein Zugang zur Nebenkostenabrechnung eingerichtet.\n"
                f"Benutzername: {u.username}\n\nBitte legen Sie über diesen Link Ihr Passwort fest (7 Tage gültig):\n"
                f"{link}\n\nViele Grüße\n{sender}")
    else:
        subject = "Passwort zurücksetzen – Nebenkostenabrechnung"
        body = (f"Hallo {u.name or u.username},\n\nüber diesen Link können Sie ein neues Passwort festlegen "
                f"(1 Stunde gültig):\n{link}\n\nFalls Sie das nicht angefordert haben, ignorieren Sie diese E-Mail.\n\n"
                f"Viele Grüße\n{sender}")
    mailer.send_mail([u.email], subject, body, [])


@router.get("/password/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return _page(request, "forgot.html", mail_ok=mailer.configured())


@router.post("/password/forgot")
async def forgot(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    login_name = str(form.get("login", "")).strip()
    ip = request.client.host if request.client else "?"
    if auth.reset_throttle.blocked(f"ip:{ip}"):
        return _redirect("/login", "Zu viele Anfragen – bitte später erneut versuchen.")
    auth.reset_throttle.hit(f"ip:{ip}")
    u = auth.find_user(s, login_name)
    if u and u.active and u.email and mailer.configured():
        try:
            send_reset_mail(request, s, u)
        except Exception:  # noqa: BLE001 – keine Auskunft, ob das Konto existiert
            pass
    return _redirect("/login", RESET_SENT)


@router.get("/password/reset", response_class=HTMLResponse)
def reset_page(request: Request, token: str = "", s: Session = Depends(get_session)):
    u = auth.user_for_reset(s, token)
    return _page(request, "reset.html", token=token, valid=u is not None, user=u)


@router.post("/password/reset")
async def reset(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    token = str(form.get("token", ""))
    u = auth.user_for_reset(s, token)
    if u is None:
        return _redirect("/password/forgot", "Link ungültig oder abgelaufen – bitte neu anfordern.")
    pw = str(form.get("password", ""))
    if problem := auth.password_problem(pw):
        return _redirect(f"/password/reset?token={quote(token)}", problem)
    if pw != str(form.get("password2", "")):
        return _redirect(f"/password/reset?token={quote(token)}", "Passwörter stimmen nicht überein.")
    auth.set_password(u, pw)
    s.commit()
    return _redirect("/login", "Passwort gespeichert – bitte anmelden.")


# --------------------------------------------------------------------------- Eigenes Konto
@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, s: Session = Depends(get_session)):
    me = current(request)
    if me is None:
        return _redirect("/login")
    return _page(request, "account.html", u=s.get(User, me.id))


@router.post("/account")
async def account_save(request: Request, s: Session = Depends(get_session)):
    me = current(request)
    if me is None:
        return _redirect("/login")
    u = s.get(User, me.id)
    form = await request.form()
    u.name = str(form.get("name", "")).strip()
    u.email = str(form.get("email", "")).strip()
    msg = "Gespeichert"
    new = str(form.get("password", ""))
    if new:
        if not auth.verify_password(str(form.get("current", "")), u.password_hash):
            return _redirect("/account", "Aktuelles Passwort falsch.")
        if problem := auth.password_problem(new):
            return _redirect("/account", problem)
        if new != str(form.get("password2", "")):
            return _redirect("/account", "Passwörter stimmen nicht überein.")
        auth.set_password(u, new)
        msg = "Passwort geändert"
    s.commit()
    resp = _redirect("/account", msg)
    if new:  # Sitzungsversion hat sich geändert → Cookie erneuern
        _set_cookie(resp, request, auth.make_cookie(s, u, 0.5), 0.5)
    return resp


# --------------------------------------------------------------------------- Benutzerverwaltung (Verwalter)
@router.get("/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def users_page(request: Request, s: Session = Depends(get_session)):
    users = s.query(User).order_by(User.role, User.username).all()
    parties = {p.id: p for p in s.query(Party).order_by(Party.sort, Party.id).all()}
    return _page(request, "users.html", users=users, parties=parties, roles=auth.ROLES, st=get_settings(s),
                 mail_ok=mailer.configured())


@router.post("/users/settings", dependencies=[Depends(require_admin)])
async def users_settings(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    save_settings(s, {"portal_auto_publish": "1" if form.get("portal_auto_publish") else ""})
    for p in s.query(Party).all():
        p.portal = bool(form.get(f"portal_{p.id}"))
    s.commit()
    return _redirect("/users", "Gespeichert")


@router.get("/users/{uid}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def user_edit(request: Request, uid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    u = User(role="tenant", active=True, party_id=party) if uid == 0 else s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if uid == 0 and party:
        p = s.get(Party, party)
        if p:
            u.name, u.email = p.name, (p.email or "").split(",")[0].strip()
    parties = s.query(Party).order_by(Party.sort, Party.id).all()
    return _page(request, "user_edit.html", u=u, uid=uid, parties=parties, roles=auth.ROLES,
                 mail_ok=mailer.configured())


def _admins(s: Session, exclude: int = 0) -> int:
    return s.query(User).filter(User.role == "admin", User.active.is_(True), User.id != exclude).count()


@router.post("/users/{uid}", dependencies=[Depends(require_admin)])
async def user_save(request: Request, uid: int, s: Session = Depends(get_session)):
    form = await request.form()
    me = current(request)
    u = User() if uid == 0 else s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if form.get("delete"):
        if me and u.id == me.id:
            return _redirect(f"/users/{uid}", "Du kannst dich nicht selbst löschen.")
        if u.role == "admin" and _admins(s, exclude=u.id) == 0:
            return _redirect(f"/users/{uid}", "Der letzte Verwalter kann nicht gelöscht werden.")
        s.delete(u)
        s.commit()
        return _redirect("/users", "Benutzer gelöscht")

    username = str(form.get("username", "")).strip()
    if not username:
        return _redirect(f"/users/{uid}", "Benutzername fehlt.")
    clash = s.query(User).filter(User.username == username, User.id != (u.id or 0)).first()
    if clash:
        return _redirect(f"/users/{uid}", f"Benutzername „{username}“ ist schon vergeben.")
    role = str(form.get("role", "tenant"))
    role = role if role in auth.ROLES else "tenant"
    active = bool(form.get("active"))
    if u.id and u.role == "admin" and (role != "admin" or not active) and _admins(s, exclude=u.id) == 0:
        return _redirect(f"/users/{uid}", "Der letzte aktive Verwalter muss Verwalter bleiben.")
    party_id = int(form.get("party_id") or 0) or None
    if role == "tenant" and not party_id:
        return _redirect(f"/users/{uid}" + (f"?party={party_id}" if party_id else ""),
                         "Mieter müssen einer Partei zugeordnet sein.")
    u.username, u.role, u.active = username, role, active
    u.name = str(form.get("name", "")).strip()
    u.email = str(form.get("email", "")).strip()
    u.party_id = party_id if role == "tenant" else None
    pw = str(form.get("password", ""))
    if pw:
        if problem := auth.password_problem(pw):
            return _redirect(f"/users/{uid}", problem)
        auth.set_password(u, pw)
    elif uid == 0:
        u.password_hash = ""  # Login erst nach Festlegen über den Einladungslink
    if uid == 0:
        s.add(u)
    s.commit()
    msg = "Benutzer gespeichert"
    if form.get("invite") or (uid == 0 and not pw):
        if not (u.email and mailer.configured()):
            msg += " – keine Einladung verschickt (E-Mail-Adresse oder Mailversand fehlt; Passwort selbst setzen)"
        else:
            try:
                send_reset_mail(request, s, u, invite=True)
                msg += f" – Einladung an {u.email} verschickt"
            except Exception as e:  # noqa: BLE001
                msg += f" – Einladung fehlgeschlagen: {e}"
    return _redirect("/users", msg)


@router.post("/users/{uid}/reset", dependencies=[Depends(require_admin)])
def user_reset(request: Request, uid: int, s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if not (u.email and mailer.configured()):
        return _redirect(f"/users/{uid}", "Keine E-Mail-Adresse hinterlegt bzw. Mailversand nicht eingerichtet.")
    try:
        send_reset_mail(request, s, u)
        return _redirect("/users", f"Link zum Zurücksetzen an {u.email} verschickt")
    except Exception as e:  # noqa: BLE001
        return _redirect(f"/users/{uid}", f"E-Mail fehlgeschlagen: {e}")


# --------------------------------------------------------------------------- Mieterportal
def _portal_party(request: Request, s: Session, party: Optional[int]) -> Optional[Party]:
    me = current(request)
    if me is not None and not me.is_admin:
        return s.get(Party, me.party_id) if me.party_id else None
    return s.get(Party, party) if party else None  # Verwalter: Vorschau „Ansicht als Mieter“


def visible_billings(s: Session, party: Party) -> list[Billing]:
    if party is None or not party.portal:
        return []
    out = []
    for b in s.query(Billing).filter(Billing.status == "final", Billing.published.is_(True)) \
            .order_by(Billing.period_start.desc()).all():
        if party_result(b, party.id) is not None:
            out.append(b)
    return out


@router.get("/portal", response_class=HTMLResponse)
def portal(request: Request, party: Optional[int] = None, s: Session = Depends(get_session)):
    me = current(request)
    p = _portal_party(request, s, party)
    rows = [(b, party_result(b, p.id)) for b in visible_billings(s, p)] if p else []
    parties = s.query(Party).order_by(Party.sort, Party.id).all() if (me is None or me.is_admin) else []
    return _page(request, "portal.html", party=p, rows=rows, parties=parties, period_text=period_text)


def _portal_billing(request: Request, s: Session, bid: int, party: Optional[int]):
    p = _portal_party(request, s, party)
    b = s.get(Billing, bid)
    if p is None or b is None or b not in visible_billings(s, p):
        raise HTTPException(404, "Abrechnung nicht gefunden")
    return b, party_result(b, p.id)


@router.get("/portal/{bid}.pdf")
def portal_pdf(request: Request, bid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    b, pr = _portal_billing(request, s, bid, party)
    return Response(invoice_pdf(b, pr), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, pr)}"', "Cache-Control": "no-store"})


@router.get("/portal/{bid}", response_class=HTMLResponse)
def portal_view(request: Request, bid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    b, pr = _portal_billing(request, s, bid, party)
    return HTMLResponse(invoice_html(b, pr))
