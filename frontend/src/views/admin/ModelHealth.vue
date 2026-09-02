<template>
  <div class="model-health-page">
    <div class="placeholder-box">
      <div class="placeholder-title">模型健康</div>
      <div class="placeholder-desc">
        显示 Embedding / Reranker / LLM 的真实运行状态（used / degraded / error_code）。
        后端模型健康 API（P13-MODEL-06）尚未实现，待公司模型服务联调后接入。
      </div>
    </div>
  </div>
</template>

<style scoped>
.model-health-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}
.placeholder-box {
  text-align: center;
  color: #6b7280;
  max-width: 480px;
}
.placeholder-title {
  font-size: 18px;
  font-weight: 600;
  color: #374151;
  margin-bottom: 10px;
}
.placeholder-desc {
  font-size: 13px;
  line-height: 1.6;
}
</style>
