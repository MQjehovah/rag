/**
 * 惰性渲染工具: 元素进入(接近)视口才执行回调, 用于 mermaid 图表/图片等重资源。
 *
 * - IntersectionObserver 不可用时立即执行回调(降级为原行为);
 * - 触发一次后自动 unobserve, 同一元素重复 observe 由调用方自行控制;
 * - `disconnect()` 供组件卸载时清理。
 */

export interface LazyObserver {
  observe(el: Element): void
  unobserve(el: Element): void
  disconnect(): void
}

export function createLazyObserver(
  onVisible: (el: Element) => void,
  rootMargin = '320px 0px',
): LazyObserver {
  if (typeof IntersectionObserver === 'undefined') {
    return {
      observe: (el) => onVisible(el),
      unobserve: () => {},
      disconnect: () => {},
    }
  }
  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue
      io.unobserve(entry.target)
      onVisible(entry.target)
    }
  }, { rootMargin })
  return {
    observe: (el) => io.observe(el),
    unobserve: (el) => io.unobserve(el),
    disconnect: () => io.disconnect(),
  }
}

/** 给容器内图片补上 loading/decoding 属性并逐个交给 observer(进入视口才加载/签名)。 */
export function observeLazyImages(root: ParentNode, observer: LazyObserver): HTMLImageElement[] {
  const imgs = Array.from(root.querySelectorAll('img'))
  for (const img of imgs) {
    img.loading = 'lazy'
    img.decoding = 'async'
    observer.observe(img)
  }
  return imgs
}
