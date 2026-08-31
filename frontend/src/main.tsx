import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { useCourseStore } from '@/stores/course'
import { ToastContainer } from '@/components/ui/Toast'
import App from '@/App'
import './styles/global.css'

function Init() {
  useCourseStore.getState().setCourses([]);

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
