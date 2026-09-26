/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Stratum dark palette (same identity as the old Streamlit shell).
        stratum: {
          bg: "#0E1116",
          panel: "#161B22",
          border: "#21262D",
          accent: "#2EE6A6",
          danger: "#FF4D4D",
          warn: "#F5C518",
          muted: "#8B949E",
        },
      },
      minHeight: { touch: "44px" }, // WCAG 2.5.5 / Apple HIG touch targets
    },
  },
  plugins: [],
};
