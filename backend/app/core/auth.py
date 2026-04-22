import logging
from typing import List, Optional

from ldap3 import Server, Connection, SUBTREE, ALL
from ldap3.core.exceptions import LDAPException

from app.config import settings

logger = logging.getLogger(__name__)


class LDAPAuthService:
    def _get_server(self) -> Server:
        return Server(settings.ldap_server_url, get_info=ALL, connect_timeout=10)

    def _service_bind(self) -> Connection:
        server = self._get_server()
        conn = Connection(
            server,
            user=settings.ldap_bind_dn,
            password=settings.ldap_bind_password,
            auto_bind=True,
            raise_exceptions=True,
        )
        return conn

    def authenticate(self, username: str, password: str) -> Optional[dict]:
        if not settings.ldap_server_url:
            return None

        try:
            conn = self._service_bind()

            search_filter = settings.ldap_user_filter.replace("{username}", username)
            conn.search(
                search_base=settings.ldap_user_base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=["uid", "mail", "displayName", "cn", "dn"],
            )

            if not conn.entries:
                conn.unbind()
                return None

            user_entry = conn.entries[0]
            user_dn = user_entry.entry_dn
            email = str(user_entry.mail.value) if hasattr(user_entry, "mail") and user_entry.mail.value else ""
            display_name = (
                str(user_entry.displayName.value)
                if hasattr(user_entry, "displayName") and user_entry.displayName.value
                else str(user_entry.cn.value)
                if hasattr(user_entry, "cn") and user_entry.cn.value
                else username
            )

            conn.unbind()

            verify_conn = Connection(
                self._get_server(),
                user=user_dn,
                password=password,
                auto_bind=True,
                raise_exceptions=True,
            )
            verify_conn.unbind()

            groups = self._fetch_groups(user_dn)

            return {
                "username": username,
                "email": email,
                "display_name": display_name,
                "groups": groups,
            }
        except LDAPException as e:
            logger.error(f"LDAP auth failed for {username}: {e}")
            return None

    def _fetch_groups(self, user_dn: str) -> List[str]:
        if not settings.ldap_group_base_dn:
            return []

        try:
            conn = self._service_bind()
            search_filter = settings.ldap_group_filter.replace("{user_dn}", user_dn)
            conn.search(
                search_base=settings.ldap_group_base_dn,
                search_filter=search_filter,
                search_scope=SUBTREE,
                attributes=["cn"],
            )

            groups = []
            for entry in conn.entries:
                if hasattr(entry, "cn") and entry.cn.value:
                    groups.append(str(entry.cn.value))

            conn.unbind()
            return groups
        except LDAPException as e:
            logger.error(f"LDAP group fetch failed for {user_dn}: {e}")
            return []


ldap_auth = LDAPAuthService()