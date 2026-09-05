"""用户相关公共工具:组同步与 payload 组装,供 jwt_utils 双轨鉴权共用。"""

import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.models.database import User, UserGroup


def sync_user_groups(db: Session, user, groups: list[str]):
    """把 user 的组全量同步为 groups(删旧插新,先删后插并提交)。

    空 groups 是否清空由调用方决定:本函数不做为空短路,调用方在无需
    改动时直接不调用即可(见 jwt_utils._resolve_sso_user)。
    """
    db.query(UserGroup).filter(UserGroup.user_id == user.id).delete()
    for g in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=user.id, group_name=g))
    db.commit()


def build_user_payload(db: Session, user: User) -> dict:
    """组装 get_current_user 返回的 dict。

    HS256 轨与 SSO 轨共用同一形状,杜绝两套 payload 漂移。
    """
    current_groups = [
        ug.group_name
        for ug in db.query(UserGroup).filter(UserGroup.user_id == user.id).all()
    ]
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_local": user.is_local,
        "is_active": user.is_active,
        "groups": current_groups,
        "is_admin": (
            settings.ldap_group_map_admin in current_groups
            if settings.ldap_group_map_admin
            else False
        ),
    }
