# 用户管理 API 错误码说明

常见错误码如下：

- HTTP 400：请求参数错误，通常是参数类型或取值范围不符合要求。
- HTTP 401：未认证或令牌无效，请检查 Authorization 请求头。
- HTTP 403：权限不足，当前令牌无权访问该资源。
- HTTP 404：资源不存在。
- HTTP 429：请求过于频繁，已被限流，请稍后重试。
- HTTP 500：服务器内部错误。

错误响应体统一包含 code 与 message 两个字段。示例：HTTP 404 返回
{"code": "USER_NOT_FOUND", "message": "用户不存在"}。

业务错误码候选：USER_NOT_FOUND、TOKEN_INVALID、PERMISSION_DENIED、RATE_LIMITED。
具体语义与必填字段以接口文档与代码为准，本文档不推断请求参数定义。
