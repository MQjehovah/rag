# 用户管理 API 认证说明

所有管理接口都必须携带有效的访问令牌。令牌通过登录接口换取，过期后需要重新登录。

## 认证方式

GET /users 使用 Bearer token 认证，在请求头中携带 Authorization。
POST /users 使用同样的 Bearer token。

当令牌缺失或失效时服务端返回 HTTP 401，错误码为 401 TOKEN_INVALID。

## 注意事项

令牌有效期默认 2 小时。不要在文档中记录明文密钥；密钥通过密钥管理系统下发。
参数类型、必填规则与请求体 Schema 以 OpenAPI 定义为准，本文不做推断。
