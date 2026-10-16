/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: {
          950: "#070b12",
          900: "#0c1220",
          800: "#121a2b",
          700: "#1a2438",
        },
        accent: {
          buy: "#3dd68c",
          sell: "#ff5d6c",
          hold: "#8b9bb4",
          gold: "#e8c872",
        },
      },
      fontFamily: {
        sans: ["IBM Plex Sans", "Segoe UI", "sans-serif"],
        mono: ["IBM Plex Mono", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};
