import { Suspense } from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
import { Outlet } from 'react-router-dom';
import { ProtectedRoute } from '@/pages/auth/ProtectedRoute';
import { useAuthStore } from '@/stores/auth';
import { Sidebar } from '@/components/layout/Sidebar';
import { Layout } from '@/components/layout/Layout';
import { lazyWithRetry } from '@/lib/lazyWithRetry';

/* 路由级代码分割：首屏只带外壳与共享 UI，九个页面按需加载，
   避免单 bundle 越过 500kB 警戒线（vite 打包警告的根治路径）。
   走 lazyWithRetry：链路偶发 RST/挂起导致 chunk 拉取失败时自动换新连接
   重试、必要时整页重载，不再让用户手动刷新。 */
const CourseSpacePage = lazyWithRetry(() => import('@/pages/course-space'));
const Dashboard = lazyWithRetry(() => import('@/pages/dashboard'));
const Materials = lazyWithRetry(() => import('@/pages/materials'));
const Framework = lazyWithRetry(() => import('@/pages/framework'));
const Knowledge = lazyWithRetry(() => import('@/pages/knowledge'));
const PaperPage = lazyWithRetry(() => import('@/pages/paper'));
/* 资料库「试卷」文件夹的归档快照编辑页（与试卷页同款双栏阅读器） */
const PaperArchivePage = lazyWithRetry(() => import('@/pages/paper-archive'));
const AssistantPage = lazyWithRetry(() => import('@/pages/assistant'));
const LoginPage = lazyWithRetry(() =>
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
          {/* 归档快照编辑：资料库「试卷」文件夹点「编辑」进来，只改副本 */}
          <Route path="paper-archive/:archiveId" element={<PaperArchivePage />} />
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
