/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./frontend/pages/**/*.html",
  ],
  theme: {
    extend: {
      colors: {
        creamsicle: {
          bg: "#fff7f0",
          primary: "#ff8c42",
          secondary: "#ff7a2f",
          soft: "#ffe5d1",
        },
      },
    },
  },
  plugins: [],
}
