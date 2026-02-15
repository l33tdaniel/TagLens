document.addEventListener("DOMContentLoaded", () => {
  const dropZone = document.getElementById("dropZone");
  const fileInput = document.getElementById("fileInput");
  const browseBtn = document.getElementById("browseBtn");
  const fileList = document.getElementById("fileList");

  browseBtn.addEventListener("click", () => {
    fileInput.click();
  });

  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("bg-orange-200");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("bg-orange-200");
  });

  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("bg-orange-200");
    handleFiles(e.dataTransfer.files);
  });

  document.addEventListener("DOMContentLoaded", async () => {
  try {
    const response = await fetch("/api/profile");

    if (!response.ok) {
      window.location.href = "/login";
      return;
    }

    const data = await response.json();

    document.getElementById("username").textContent = data.username;
    document.getElementById("email").textContent = data.email;
    document.getElementById("created").textContent =
      "Member since " + new Date(data.created_at).toLocaleDateString();

  } catch (error) {
    console.error("Failed to load profile data:", error);
  }
  });

  fileInput.addEventListener("change", () => {
    handleFiles(fileInput.files);
  });

  function handleFiles(files) {
    fileList.innerHTML = "";

    [...files].forEach(file => {
      if (!isValidType(file)) {
        alert(`Invalid file type: ${file.name}`);
        return;
      }

      const div = document.createElement("div");
      div.className = "bg-orange-50 p-2 rounded";
      div.textContent = file.name;
      fileList.appendChild(div);
    });
  }

  function isValidType(file) {
    const allowedTypes = ["image/png", "image/heic"];
    return allowedTypes.includes(file.type);
  }
});
