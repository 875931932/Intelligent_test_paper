import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ToastContainer } from '@/components/ui/Toast'
import App from '@/App'
import './styles/global.css'

function Init() {
  // 课程由 course store 在演示模式下自动注入静态数据，无需在此清空

  return (
    <BrowserRouter>
      <App />
      <ToastContainer />
    </BrowserRouter>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Init />
  </StrictMode>,
)
