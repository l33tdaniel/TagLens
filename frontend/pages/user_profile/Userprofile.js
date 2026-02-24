document.addEventListener("DOMContentLoaded", () => {
  const dropZone = document.getElementById("dropZone");
  const fileInput = document.getElementById("fileInput");
  const browseBtn = document.getElementById("browseBtn");
  const fileList = document.getElementById("fileList");
  const galleryGrid = document.getElementById("galleryGrid");
  const photosCount = document.getElementById("photosCount");

  browseBtn.addEventListener("click", (event) => {
    event.preventDefault();
    fileInput.click();
  });

  dropZone.addEventListener("dragover", (event) => {
    event.preventDefault();
    dropZone.classList.add("bg-orange-200");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("bg-orange-200");
  });

  dropZone.addEventListener("drop", async (event) => {
    event.preventDefault();
    dropZone.classList.remove("bg-orange-200");
    await handleFiles(event.dataTransfer.files);
  });

  fileInput.addEventListener("change", async () => {
    await handleFiles(fileInput.files);
  });

  loadProfile();

  async function loadProfile() {
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
      renderGallery(data.photos || []);
    } catch (error) {
      console.error("Failed to load profile data:", error);
    }
  }

  async function handleFiles(files) {
    const validFiles = [...files].filter(isValidType);
    fileList.innerHTML = "";

    if (validFiles.length === 0) {
      fileList.textContent = "No valid files selected.";
      return;
    }

    for (const file of validFiles) {
      const status = document.createElement("div");
      status.className = "bg-orange-50 p-2 rounded";
      status.textContent = `Uploading ${file.name}...`;
      fileList.appendChild(status);

      try {
        const imageBase64 = await fileToBase64(file);
        const uploadResponse = await fetch("/api/photos", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: file.name,
            image_base64: imageBase64,
          }),
        });

        if (!uploadResponse.ok) {
          const payload = await uploadResponse.json().catch(() => ({}));
          status.textContent = `Failed ${file.name}: ${payload.error || "upload failed"}`;
          continue;
        }

        const savedPhoto = await uploadResponse.json();
        status.textContent = `${file.name}: complete`;
        prependPhotoCard(savedPhoto);
      } catch (error) {
        status.textContent = `Failed ${file.name}: ${String(error)}`;
      }
    }
  }

  function isValidType(file) {
    const allowedTypes = ["image/png", "image/jpeg", "image/heic", "image/webp"];
    return allowedTypes.includes(file.type);
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const dataUrl = String(reader.result || "");
        const base64Content = dataUrl.split(",")[1] || "";
        resolve(base64Content);
      };
      reader.onerror = () => reject(new Error("Could not read file"));
      reader.readAsDataURL(file);
    });
  }

  function renderGallery(photos) {
    galleryGrid.innerHTML = "";
    for (const photo of photos) {
      galleryGrid.appendChild(buildPhotoCard(photo));
    }
    photosCount.textContent = String(photos.length);
  }

  function prependPhotoCard(photo) {
    galleryGrid.prepend(buildPhotoCard(photo));
    const count = Number(photosCount.textContent || "0") + 1;
    photosCount.textContent = String(count);
  }

  function buildPhotoCard(photo) {
    const card = document.createElement("div");
    card.className = "border border-orange-200 bg-white rounded-xl p-4";

    const title = document.createElement("p");
    title.className = "text-sm font-semibold text-gray-900";
    title.textContent = photo.filename || "Untitled";

    const description = document.createElement("p");
    description.className = "mt-2 text-sm text-gray-600";
    description.textContent = photo.description || "No AI description available.";

    const timestamp = document.createElement("p");
    timestamp.className = "mt-2 text-xs text-gray-400";
    timestamp.textContent = new Date(photo.created_at).toLocaleString();

    card.appendChild(title);
    card.appendChild(description);
    card.appendChild(timestamp);
    return card;
  }
});
