/* 临时预览入口：上传第三方证书弹窗，不参与构建。 */
import "./preview-lang";
import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { I18nProvider, ensureDict } from "./i18n";
import { CertUploadDialog } from "./components/CertUploadDialog";

import "./styles/theme.css";
import "./styles/global.css";
import "./styles/components.css";
import "./styles/layout.css";
import "./styles/responsive.css";

function App() {
  const [, setTick] = useState(0);
  useEffect(() => {
    void ensureDict("settings").then(() => setTick((v) => v + 1));
  }, []);
  return (
    <div style={{ minHeight: "100vh", background: "var(--bg)" }}>
      <CertUploadDialog
        open
        busy={false}
        onClose={() => undefined}
        onSubmit={async () => false}
      />
    </div>
  );
}

try {
  localStorage.setItem("pve_lang", "zh-CN");
} catch {
  /* 忽略 */
}

createRoot(document.getElementById("root")!).render(
  <I18nProvider>
    <App />
  </I18nProvider>,
);
