import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.api.deps import get_db
from app.core.sso_auth import SsoAuthError, verify_sso_token
from app.core.user_utils import build_user_payload, sync_user_groups
from app.models.database import User

ALGORITHM = "HS256"

security = HTTPBearer()


def create_access_token(user_id: str, groups: List[str]) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": user_id, "groups": groups, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """双轨鉴权:先认自家 HS256 token,失败再试 SSO OIDC(RS256) token。

    轨1(HS256)保持原逻辑:sub=user UUID,禁用/不存在 -> 401。
    轨2(SSO) sub=工号:用户不存在自动建号并同步 groups;
    已禁用 -> 403。两轨最终经 build_user_payload 输出同形状 dict。
    """
    payload = decode_token(credentials.credentials)
    if payload is not None:
        return _resolve_hs256_user(db, payload)

    try:
        claims = verify_sso_token(credentials.credentials)
    except SsoAuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
        )
    return _resolve_sso_user(db, claims)


def _resolve_hs256_user(db: Session, payload: dict) -> dict:
    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已禁用",
        )
    return build_user_payload(db, user)


def _resolve_sso_user(db: Session, claims: dict) -> dict:
    """SSO 轨:按 username=工号 查/建 User。

    已禁用 -> 403;已有用户把 email/name 回写为 claims 值(SSO 为权威源,
    缺失字段保持原值,与 LDAP login 分支一致);有 groups 声明才同步。
    """
    username = str(claims["sub"])
    user = db.query(User).filter(User.username == username).first()

    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            email=claims.get("email", ""),
            display_name=claims.get("name", username),
            is_local=False,
            is_active=True,
        )
        db.add(user)
        try:
            db.commit()
            db.refresh(user)
        except IntegrityError:
            # 并发建号竞态:username 唯一约束被别的请求抢建,回滚后回查兜底
            db.rollback()
            user = db.query(User).filter(User.username == username).first()
            if user is None:
                raise

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已禁用",
        )

    _write_back_claims(db, user, claims)
    groups = _normalize_claims_groups(claims)
    if groups:
        sync_user_groups(db, user, groups)
    return build_user_payload(db, user)


def _write_back_claims(db: Session, user: User, claims: dict) -> None:
    """把 claims 的 email/name 回写到已有用户,缺失字段保持原值。

    新建用户建号时已按 claims 填充,此处通常为空操作;仅在有变化时提交,
    避免每个 SSO 请求都产生无谓 commit。
    """
    email = claims.get("email", user.email)
    display_name = claims.get("name", user.display_name)
    if email != user.email or display_name != user.display_name:
        user.email = email
        user.display_name = display_name
        db.commit()


def _normalize_claims_groups(claims: dict) -> list[str]:
    """把 claims 的 groups 归一化为清洗后的组名列表。

    IdP 可能给 list[str] 或逗号分隔字符串;其余类型一律视为缺失。
    list[str] 元素会去空白、去空;逗号字符串按 "," 切分后同样清洗。
    """
    groups = claims.get("groups")
    if isinstance(groups, str):
        return [g.strip() for g in groups.split(",") if g.strip()]
    if isinstance(groups, list):
        return [g.strip() for g in groups if isinstance(g, str) and g.strip()]
    return []
