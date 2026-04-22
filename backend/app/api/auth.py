from fastapi import APIRouter, HTTPException, Depends, status
from sqlalchemy.orm import Session
from passlib.context import CryptContext
import uuid

from app.models.database import User, UserGroup, get_session, get_engine, init_db
from app.models.schema import LoginRequest, LoginResponse, UserResponse, GroupResponse
from app.core.auth import ldap_auth
from app.core.jwt_utils import create_access_token, get_current_user
from app.config import settings

router = APIRouter(prefix="/api/auth", tags=["认证"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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


def _sync_user_groups(db: Session, user_id: str, groups: list[str]):
    db.query(UserGroup).filter(UserGroup.user_id == user_id).delete()
    for g in groups:
        db.add(UserGroup(id=str(uuid.uuid4()), user_id=user_id, group_name=g))
    db.commit()


def _create_local_admin(db: Session):
    existing = db.query(User).filter(User.is_local == True).first()
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
    db.add(UserGroup(id=str(uuid.uuid4()), user_id=admin.id, group_name="__local_admin__"))
    db.commit()

    if not settings.local_admin_password:
        import logging
        logging.getLogger(__name__).warning(
            f"Local admin created — username: {settings.local_admin_username}, password: {password}"
        )


@router.on_event("startup")
def startup():
    db = get_db()
    _create_local_admin(db)


@router.post("/login", response_model=LoginResponse)
async def login(data: LoginRequest, db: Session = Depends(get_db)):
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
        groups = [ug.group_name for ug in db.query(UserGroup).filter(UserGroup.user_id == user.id).all()]
        token = create_access_token(user.id, groups)
        return LoginResponse(
            token=token,
            user=UserResponse(
                id=user.id,
                username=user.username,
                email=user.email,
                display_name=user.display_name,
                is_local=True,
                groups=groups,
            ),
        )

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
    _sync_user_groups(db, user.id, groups)

    token = create_access_token(user.id, groups)
    return LoginResponse(
        token=token,
        user=UserResponse(
            id=user.id,
            username=user.username,
            email=user.email,
            display_name=user.display_name,
            is_local=False,
            groups=groups,
        ),
    )


@router.get("/me", response_model=UserResponse)
async def get_me(current_user=Depends(get_current_user), db: Session = Depends(get_db)):
    return UserResponse(**current_user)


@router.get("/groups", response_model=list[GroupResponse])
async def get_groups(current_user=Depends(get_current_user)):
    return [GroupResponse(group_name=g) for g in current_user["groups"]]
