import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import ptPT from "./locales/pt-PT.json";

void i18n.use(initReactI18next).init({
  resources: {
    "pt-PT": { translation: ptPT },
  },
  lng: "pt-PT",
  fallbackLng: "pt-PT",
  supportedLngs: ["pt-PT"],
  interpolation: { escapeValue: false },
  returnNull: false,
});

export default i18n;
