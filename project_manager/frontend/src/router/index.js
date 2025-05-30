import Vue from "vue";
import VueRouter from "vue-router";
import ProjectDashboard from "@/pages/ProjectDashboard.vue";
import ProjectDetail from "@/pages/ProjectDetailV2.vue";
import LoginPage from "@/pages/LoginPage.vue";
import CreatAccountPage from "@/pages/CreateAccountPage.vue";
import NotFoundPage from "@/pages/NotFoundPage.vue";
import VisualizationPage from "@/pages/VisualizationPage.vue";
import DataLebeling from "@/pages/DataLebeling.vue";
import DatasetManagement from "@/pages/DatasetManagement.vue";
import TargetManagement from "@/pages/TargetManagementV2.vue";

import Cookies from "universal-cookie";

Vue.use(VueRouter);

const routes = [
  {
    path: "/",
    redirect: "/project"
  },
  {
    path: "/login",
    component: LoginPage,
    meta: { permission: "guest" }
  },
  {
    path: "/create-account",
    component: CreatAccountPage,
    meta: { permission: "guest" }
  },
  {
    path: "/project",
    name: "projects",
    component: ProjectDashboard
  },
  {
    path: "/project/:id",
    name: "ProjectDetail",
    component: ProjectDetail
  },
  {
    path: "/target",
    name: "targets",
    component: TargetManagement
  },
  {
    path: "/dataset",
    name: "dataset",
    component: DatasetManagement
  },
  {
    path: "/labeling",
    name: "labeling",
    component: DataLebeling
  },
  {
    path: "/visualization",
    name: "Visualization",
    component: VisualizationPage
  },
  {
    path: "*",
    name: "Not Found",
    component: NotFoundPage
  }
];

const router = new VueRouter({
  mode: "history",
  base: process.env.BASE_URL,
  routes
});

// /* 웹 브라우저 쿠키 정보 유무 확인 */
const isToken = () => new Cookies().get("TANGO_TOKEN");
const isUser = () => new Cookies().get("userinfo");

router.beforeEach(async (to, from, next) => {
  const { permission } = to.meta;

  if (permission === undefined)
    if (!!isToken() === false || !!isUser() === false) {
      next("/login");
    } else {
      next();
    }
  else {
    if (!!isToken() === false || !!isUser() === false) {
      next();
    } else {
      next("/");
    }
  }
});

export default router;
