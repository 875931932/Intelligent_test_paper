import { request, uploadBinary } from './http';
import { authApi } from './domains/auth';
import { coursesApi } from './domains/courses';
import { blueprintsApi, generationApi } from './domains/blueprints';
import { knowledgeRunApi } from './domains/knowledgeRun';
import { knowledgePublishApi } from './domains/knowledgePublish';
import { knowledgeViewApi } from './domains/knowledgeView';
import { materialsApi } from './domains/materials';
import { frameworkApi } from './domains/framework';
import { examProjectsApi } from './domains/examProjects';
import { paperVersionsApi } from './domains/paperVersions';

export const api = {
  health: () => request('/health'),
  auth: authApi,
  courses: coursesApi,
  blueprints: blueprintsApi,
  generation: generationApi,
  knowledge: { ...knowledgeRunApi, ...knowledgePublishApi, ...knowledgeViewApi },
  materials: materialsApi,
  framework: frameworkApi,
  examProjects: examProjectsApi,
  paperVersions: paperVersionsApi,
  uploadBinary,
};