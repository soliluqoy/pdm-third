/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Dark, EV-app inspired palette
        ink: {
          950: "#0B0E14", // app background
          900: "#10141D", // surface
          850: "#151B27", // raised surface
          800: "#1B2331", // hover
        },
        line: "rgba(148,163,184,0.10)",
        accent: {
          DEFAULT: "#5B9BFF",
          soft: "rgba(91,155,255,0.14)",
        },
        ok: "#34D399",
        warn: "#FBBF24",
        bad: "#F87171",
        off: "#64748B",
        muted: "#8B94A7",
      },
      fontFamily: {
        sans: [
          "Inter", "ui-sans-serif", "system-ui", "-apple-system",
          "Segoe UI", "Roboto", "Helvetica Neue", "Arial", "sans-serif",
        ],
      },
      borderRadius: {
        xl2: "1.25rem",
      },
    },
  },
  plugins: [],
};
