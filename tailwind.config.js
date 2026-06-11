/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./src/evk/ui/templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter var", "Inter", "ui-sans-serif", "system-ui"],
        display: ["Fraunces", "Inter var", "serif"],
      },
      colors: {
        brand: {
          50: "#fff6ef",
          100: "#ffe5d2",
          200: "#ffc7a1",
          300: "#ff9b63",
          400: "#ff7138",
          500: "#f1561f",
          600: "#d43f12",
          700: "#ae3213",
          800: "#8b2a16",
          900: "#6d2617",
          950: "#3b1009",
        },
      },
      boxShadow: {
        glow: "0 24px 60px -24px rgba(15, 23, 42, 0.28)",
      },
    },
  },
  plugins: [],
};
