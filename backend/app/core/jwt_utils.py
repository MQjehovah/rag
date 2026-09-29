import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import func
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

    已禁用 -> 403;新建账号按 claims 填充 name(空回退工号)/work_id/phone/department/email;
    已有账号按 SSO 权威源回写 name/phone/department(非空才写)/work_id(恒对齐 sub),
    email 保持原语义;有 groups 声明才同步。
    """
    username = str(claims["sub"])
    raw_email = (claims.get("email") or "").strip() or None
    claim_email = raw_email.lower() if raw_email else None

    # 邮箱优先: 与系统自建账号(本地注册/按邮箱登录的)对齐, 避免同一人两份账号;
    # 邮箱缺失时退回按工号匹配(SSO 侧未取到 LDAP mail 的极少数情况)。
    user = None
    if claim_email:
        user = db.query(User).filter(func.lower(User.email) == claim_email).first()
    if user is None:
        user = db.query(User).filter(User.username == username).first()

    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            email=claims.get("email", ""),
            name=(claims.get("name") or "").strip() or username,
            work_id=username,
            phone=(claims.get("mobile") or "").strip(),
            department=_claim_dept(claims),
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
            if user is None and claim_email:
                # 也可能是邮箱撞了并发/已存在账号, 再按邮箱回查一次
                user = db.query(User).filter(func.lower(User.email) == claim_email).first()
            if user is None:
                raise

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已禁用",
        )

    _write_back_claims(db, user, claims, username)
    groups = _normalize_claims_groups(claims)
    if groups:
        sync_user_groups(db, user, groups, source="sso")
    return build_user_payload(db, user)


def _claim_dept(claims: dict) -> str:
    """部门 claim 归一为去空白字符串(首选 dept, 兼容 department 别名)。

    取值来源与 _normalize_claims_groups 一致(dept 优先, 兼容 department 别名);
    list 型 claim 取首个非空元素, 非 str/list 视为缺失。
    仅用于 users.department 展示列, 可见性组不受影响。
    """
    raw = claims.get("dept")
    if not raw:
        raw = claims.get("department")
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _write_back_claims(db: Session, user: User, claims: dict, username: str) -> None:
    """把 claims 的资料字段回写到已有用户:非空才写、变化才 commit。

    - name/phone:claim 缺失或空白时保留库中原值(管理员手工维护的值不被清空);
    - department:同 name/phone 语义(dept 缺失或空白时保留);
    - work_id:恒对齐 SSO sub(工号),为空或漂移都校正;
    - email:保持原语义(claims 里有值即覆盖)。
    新建用户建号时已按 claims 填充,此处通常为空操作,避免每个 SSO 请求都产生无谓 commit。
    """
    email = claims.get("email", user.email)
    claim_name = (claims.get("name") or "").strip()
    claim_phone = (claims.get("mobile") or "").strip()
    claim_dept = _claim_dept(claims)
    changed = False
    if email != user.email:
        user.email = email
        changed = True
    if claim_name and user.name != claim_name:
        user.name = claim_name
        changed = True
    if claim_phone and (user.phone or "") != claim_phone:
        user.phone = claim_phone
        changed = True
    if claim_dept and (user.department or "") != claim_dept:
        user.department = claim_dept
        changed = True
    if (user.work_id or "") != username:
        user.work_id = username
        changed = True
    if changed:
        db.commit()


def _normalize_claims_groups(claims: dict) -> list[str]:
    """把 claims 的 groups/roles/dept 归一化为清洗后的组名列表。

    IdP 可能给 list[str] 或逗号分隔字符串;其余类型一律视为缺失。
    list[str] 元素会去空白、去空;逗号字符串按 "," 切分后同样清洗。
    SSO 统一认证签发的是 roles,故一并采纳;roles 含 admin 时补 rag 内部
    管理员标记 __local_admin__(全库管理端点均以该组名判定)与配置的
    ldap_group_map_admin 组,使 SSO 管理员与本地/LDAP 管理员同权。

    **dept 即 group**(2026-09-25 拍板):SSO 目前只签发 dept(部门),不发 groups;
    故把 dept/department 也作为可见性组名纳入,使「部门 = 知识可见性组」,
    与 LDAP 登录轨的 groups 语义统一(不再新增 groups claim,由消费端归一)。
    """

    def _as_list(value) -> list[str]:
        if isinstance(value, str):
            return [g.strip() for g in value.split(",") if g.strip()]
        if isinstance(value, list):
            return [g.strip() for g in value if isinstance(g, str) and g.strip()]
        return []

    groups = _as_list(claims.get("groups"))
    for role in _as_list(claims.get("roles")):
        if role not in groups:
            groups.append(role)
    # dept 即 group: 部门作为可见性组名纳入(SSO 只发 dept, 不发 groups)
    for dept in _as_list(claims.get("dept")) + _as_list(claims.get("department")):
        if dept and dept not in groups:
            groups.append(dept)
    if "admin" in groups:
        if "__local_admin__" not in groups:
            groups.append("__local_admin__")
        if settings.ldap_group_map_admin and settings.ldap_group_map_admin not in groups:
            groups.append(settings.ldap_group_map_admin)
    return groups
