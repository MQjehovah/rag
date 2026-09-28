from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import logging
import urllib.parse
import uuid

from app.models.database import User, UserGroup, get_session, get_engine, init_db
from app.models.schema import LoginRequest, LoginResponse, UserResponse, GroupResponse
from app.core.auth import ldap_auth
from app.api.deps import get_db
from app.core import sso_auth
from app.core.jwt_utils import create_access_token, get_current_user, _resolve_sso_user
from app.core.user_utils import build_user_payload, sync_user_groups
from app.core.rbac_seed import seed_rbac, INTERNAL_ADMIN_GROUP
from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["认证"])

logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def _create_local_admin(db: Session):
    existing = db.query(User).filter(User.is_local).first()
    if existing:
        return

    password = settings.local_admin_password
    if not password:
        import secrets
        password = secrets.token_urlsafe(16)

    hashed = pwd_context.hash(password)
    admin = User(
        id=str(uuid.uuid4()),
        username=settings.local_admin_username,
        is_local=True,
        password_hash=hashed,
        is_active=True,
    )
    db.add(admin)
    db.add(UserGroup(id=str(uuid.uuid4()), user_id=admin.id, group_name=INTERNAL_ADMIN_GROUP))
    db.commit()

    if not settings.local_admin_password:
        logger.warning(
            f"Local admin created — username: {settings.local_admin_username}, password: {password}"
        )


@router.on_event("startup")
def startup():
    engine = get_engine(settings.database_url)
    init_db(engine)
    db = get_session(engine)
    try:
        _create_local_admin(db)
    finally:
        db.close()
    try:
        seed_rbac(engine)
    except Exception:
        # seed 不是启动前提,失败不阻断(参考 database.py 的失败不阻断模式)
        logger.warning("RBAC seed 失败", exc_info=True)


@router.post("/login", response_model=LoginResponse)
def login(data: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == data.username).first()

    if user and user.is_local:
        if not pwd_context.verify(data.password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="用户已禁用",
            )
        payload = build_user_payload(db, user)
        token = create_access_token(user.id, payload["groups"])
        return LoginResponse(token=token, user=UserResponse(**payload))

    ldap_result = ldap_auth.authenticate(data.username, data.password)
    if ldap_result is None:
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="用户名或密码错误",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LDAP认证失败",
        )

    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            username=data.username,
            email=ldap_result.get("email", ""),
            display_name=ldap_result.get("display_name", data.username),
            is_local=False,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.email = ldap_result.get("email", user.email)
        user.display_name = ldap_result.get("display_name", user.display_name)
        db.commit()

    groups = ldap_result.get("groups", [])
    sync_user_groups(db, user, groups, source="ldap")

    payload = build_user_payload(db, user)
    token = create_access_token(user.id, payload["groups"])
    return LoginResponse(token=token, user=UserResponse(**payload))


@router.get("/me", response_model=UserResponse)
def get_me(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    return UserResponse(**current_user)


@router.get("/groups", response_model=list[GroupResponse])
def get_groups(current_user=Depends(get_current_user)):
    return [GroupResponse(group_name=g) for g in current_user["groups"]]


@router.get("/users")
def list_users(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    """用户列表(用于 @ 提及)。"""
    rows = db.query(User).order_by(User.username.asc()).limit(500).all()
    return [
        {"id": u.id, "username": u.username, "display_name": u.display_name or u.username}
        for u in rows
    ]


# ---- 浏览器 SSO 登录(授权码流程) ----

def _login_redirect(error: str = "") -> RedirectResponse:
    """出错时回到前端登录页(带 error 文案),不把 JSON 错误丢给浏览器。"""
    target = settings.sso_redirect_target or "/login"
    if error:
        sep = "&" if "?" in target else "?"
        target = f"{target}{sep}error={urllib.parse.quote(error)}"
    return RedirectResponse(target, status_code=302)


@router.get("/sso/start")
def sso_start():
    """生成一次性 state 并 302 跳转到 SSO authorize 页。"""
    if not sso_auth.sso_login_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSO 登录未启用",
        )
    state = sso_auth.new_state()
    try:
        url = sso_auth.build_authorize_url(state)
    except sso_auth.SsoAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    return RedirectResponse(url, status_code=302)


@router.get("/oidc/callback")
def oidc_callback(code: str = "", state: str = "", db: Session = Depends(get_db)):
    """SSO 回调: code→id_token→校验→查/建用户→签本系统 token→302 回前端。

    id_token 的 aud 是本系统 client_id(SSO 资源轨的 sso_audience 是调用方
    client_id),故显式传受众;用户建号/回写/组同步复用 SSO 资源轨的
    _resolve_sso_user,保证两条路径行为一致。
    """
    if not sso_auth.sso_login_enabled():
        return _login_redirect("SSO 登录未启用")
    if not code or not state:
        return _login_redirect("SSO 回调参数缺失")
    if not sso_auth.validate_state(state):
        return _login_redirect("SSO 登录状态已失效,请重试")
    try:
        id_token = sso_auth.exchange_code(code)
        claims = sso_auth.verify_sso_token(id_token, audience=settings.sso_client_id)
    except sso_auth.SsoAuthError as e:
        return _login_redirect(f"SSO 登录失败: {e}")
    try:
        user_info = _resolve_sso_user(db, claims)
    except HTTPException as e:
        return _login_redirect(str(e.detail))
    token = create_access_token(user_info["id"], user_info["groups"])
    target = settings.sso_redirect_target or "/login"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}sso_token={urllib.parse.quote(token)}", status_code=302
    )
