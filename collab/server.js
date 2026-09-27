/**
 * Yjs 协同 WebSocket 服务(y-websocket 协议)。
 *
 * 房间名取 URL 路径(如 /api/collab/<room>);文档常驻内存即可——
 * Markdown 仍由前端保存到 pages.content 作为事实来源,Yjs 只用于在线合并。
 * 普通 HTTP 请求(如健康检查)一律返回 200。
 */
const http = require('http')
const { WebSocketServer } = require('ws')
const { setupWSConnection } = require('y-websocket/bin/utils')

const port = parseInt(process.env.PORT || '1234', 10)
const host = process.env.HOST || '0.0.0.0'

const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'text/plain; charset=utf-8' })
  res.end('collab-ok')
})

const wss = new WebSocketServer({ server })

wss.on('connection', (conn, req) => {
  setupWSConnection(conn, req, { gc: true })
})

server.listen(port, host, () => {
  console.log(`[collab] y-websocket listening on ${host}:${port}`)
})
