import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      // 开发代理：/api → 后端 FastAPI（生产由同机静态托管或反代处理）
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  // 单测：跑纯函数与小组件（jsdom 环境，不依赖后端）
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
  },
})
