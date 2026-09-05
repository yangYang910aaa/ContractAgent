import { createApp } from 'vue'
import './style.css' // 全局设计令牌与基础组件样式（必须先于 App 引入，保证覆盖）
import App from './App.vue'

// 单页入口：挂载根组件（视图切换见 App.vue，未引入路由）
createApp(App).mount('#app')
