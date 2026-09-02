<template>
  <el-dropdown trigger="click" @command="onCommand">
    <span class="user-trigger">
      <span class="user-avatar">{{ (displayName || '?')[0] }}</span>
      <span class="user-name">{{ displayName }}</span>
    </span>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item command="profile">个人信息</el-dropdown-item>
        <template v-if="visibleAdminItems.length">
          <el-dropdown-item divided disabled>系统工具</el-dropdown-item>
          <el-dropdown-item v-for="item in visibleAdminItems" :key="item.key" :command="item.route">
            {{ item.label }}
          </el-dropdown-item>
        </template>
        <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { ADMIN_MENU } from '../../navigation/config'
import { hasRole } from '../../navigation/permissions'

const props = withDefaults(defineProps<{
  adminItems?: typeof ADMIN_MENU
}>(), {
  adminItems: () => ADMIN_MENU,
})

const router = useRouter()
const authStore = useAuthStore()
const displayName = computed(() => authStore.user?.display_name || authStore.user?.username || '')

/** 按角色过滤系统工具菜单。 */
const visibleAdminItems = computed(() =>
  (props.adminItems || []).filter((item) => hasRole(authStore.user, item.roles))
)

function onCommand(command: string) {
  if (command === 'logout') {
    authStore.logout()
    router.push('/login')
  } else if (command === 'profile') {
    // 个人信息页后续接入
  } else {
    router.push(command)
  }
}
</script>

<style scoped>
.user-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 8px;
  transition: background 0.15s;
}
.user-trigger:hover {
  background: rgba(255, 255, 255, 0.08);
}
.user-avatar {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #38bdf8, #6366f1);
  color: #fff;
  font-size: 13px;
  font-weight: 600;
  display: flex;
  align-items: center;
  justify-content: center;
}
.user-name {
  font-size: 13px;
  color: #94a3b8;
}
</style>
