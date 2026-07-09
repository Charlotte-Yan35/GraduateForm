import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  // 部署到 GitHub Pages 项目页 https://<user>.github.io/GraduateForm/
  base: '/GraduateForm/',
  plugins: [react(), tailwindcss()],
})
