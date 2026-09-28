import { lazy, Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Outlet } from 'react-router-dom';
import { ProtectedRoute } from '@/pages/auth/ProtectedRoute';
import { useAuthStore } from '@/stores/auth';
import { Sidebar } from '@/components/layout/Sidebar';
import { Layout } from '@/components/layout/Layout';

/* 路由级代码分割：首屏只带外壳与共享 UI，八个页面按需加载，
   避免单 bundle 越过 500kB 警戒线（vite 打包警告的根治路径） */
const CourseSpacePage = lazy(() => import('@/pages/course-space'));
const Dashboard = lazy(() => import('@/pages/dashboard'));
const Materials = lazy(() => import('@/pages/materials'));
const Framework = lazy(() => import('@/pages/framework'));
const Knowledge = lazy(() => import('@/pages/knowledge'));
const PaperPage = lazy(() => import('@/pages/paper'));
const AssistantPage = lazy(() => import('@/pages/assistant'));
const LoginPage = lazy(() =>
  import('@/pages/auth/LoginPage').then((m) => ({ default: m.LoginPage }))
);

/** 懒加载路由占位：由最近的 Suspense 边界接管（内容区或独立页） */
function RouteFallback() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '260px' }}>
      <div className="spinner spinner-lg" />
    </div>
  );
}

function AppShell() {
  const logout = useAuthStore((s) => s.logout);

  return (
    <Layout sidebar={<Sidebar onLogout={logout} />}>
      {/* 内容区独立边界：切页懒加载时侧栏保持在场，只换内容 */}
      <Suspense fallback={<RouteFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/courses"
          element={
            <ProtectedRoute>
              <CourseSpacePage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/courses/:courseId"
          element={
            <ProtectedRoute>
              <AppShell />
            </ProtectedRoute>
          }
        >
          <Route index element={<Dashboard />} />
          <Route path="materials" element={<Materials />} />
          <Route path="framework" element={<Framework />} />
          <Route path="knowledge" element={<Knowledge />} />
          {/* 出卷流水线与试卷查看/审核/导出已合并为同一个「试卷」页面 */}
          <Route path="paper" element={<PaperPage />} />
          {/* AI 助手对话页：问答/查询流式回复，写操作提案卡确认制 */}
          <Route path="assistant" element={<AssistantPage />} />
          <Route path="exam-projects" element={<Navigate to="../paper" replace />} />
          <Route path="paper-center" element={<Navigate to="../paper" replace />} />
        </Route>
        <Route path="*" element={<Navigate to="/courses" replace />} />
      </Routes>
    </Suspense>
  );
}
