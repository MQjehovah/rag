from datetime import datetime, timedelta, timezone
from typing import List, Optional

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.config import settings
from app.core import access_control
from app.models.database import User, UserGroup

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
        # 统一判定：委托统一权限服务（本地管理员或 LDAP/SSO 管理员组）
        "is_admin": access_control.is_admin({"groups": current_groups}),
    }


def get_visible_page_ids(db: Session, current_user: dict) -> set:
    """当前用户可见的 Page id 集合（W13 越权修复，Phase B 统一到 access_control）。"""
    return access_control.get_visible_page_ids(db, current_user)


def is_admin_user(current_user: dict) -> bool:
    """Return whether the authenticated user may bypass content ACL checks.

    委托统一权限服务：本地试点管理员（__local_admin__）或正式 LDAP/SSO 管理员组。
    """
    return access_control.is_admin(current_user)


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """统一的 FastAPI 依赖：仅管理员可访问（P8-BE-01）。

    各 API 模块原先各自实现 `_require_admin`（重复且签名不一），
    统一收敛到此依赖，避免遗漏鉴权。非管理员 → 403。
    """
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="仅管理员可执行此操作")
    return current_user
