/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Light, console-dashboard palette (Agent Router style)
        ink: {
          950: "#F3F5FA", // app background
          900: "#FFFFFF", // surface / cards
          850: "#F5F7FB", // raised surface / subtle fills
          800: "#ECF0F7", // hover / chip background
        },
        line: "rgba(15,23,42,0.08)",
        accent: {
          DEFAULT: "#10B981", // teal-green brand (nav active, primary buttons)
          soft: "rgba(16,185,129,0.12)",
        },
        brand: {
          DEFAULT: "#3B82F6", // blue (tabs, links, info)
          soft: "rgba(59,130,246,0.10)",
        },
        ok: "#16A34A",
        warn: "#D97706",
        bad: "#DC2626",
        off: "#94A3B8",
        muted: "#7B8499",
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
