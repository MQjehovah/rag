from datetime import datetime, timedelta, timezone
from typing import List, Optional

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import User, UserGroup, get_session, get_engine, init_db

ALGORITHM = "HS256"

_engine = None
_session = None


def get_db():
    global _engine, _session
    if _engine is None:
        _engine = get_engine(settings.database_url)
        init_db(_engine)
    if _session is None:
        _session = get_session(_engine)
    return _session


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
    payload = decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证令牌",
        )

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

    user_groups = db.query(UserGroup).filter(UserGroup.user_id == user.id).all()
    current_groups = [ug.group_name for ug in user_groups]

    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_local": user.is_local,
        "is_active": user.is_active,
        "groups": current_groups,
        "is_admin": settings.ldap_group_map_admin in current_groups if settings.ldap_group_map_admin else False,
    }
