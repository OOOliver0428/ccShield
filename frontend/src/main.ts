import { createApp } from "vue";
import { createPinia } from "pinia";
import "element-plus/dist/index.css";
import "./styles/tokens.css";
import "./styles/global.css";
import App from "./App.vue";

const app = createApp(App);
app.use(createPinia());
app.mount("#app");
