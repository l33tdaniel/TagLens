document.addEventListener("DOMContentLoaded", () => {
  const dropZone = document.getElementById("dropZone");
  const fileInput = document.getElementById("fileInput");
  const browseBtn = document.getElementById("browseBtn");
  const fileList = document.getElementById("fileList");
  const galleryGrid = document.getElementById("galleryGrid");
  const photosCount = document.getElementById("photosCount");
  const sortBy = document.getElementById("sortBy");
  const sortOrder = document.getElementById("sortOrder");
  let currentPhotos = [];

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

  sortBy.addEventListener("change", () => {
    loadProfile();
  });

  sortOrder.addEventListener("change", () => {
    loadProfile();
  });

  loadProfile();

  async function loadProfile() {
    try {
      const params = new URLSearchParams({
        sort_by: sortBy.value,
        order: sortOrder.value,
      });
      const response = await fetch(`/api/profile?${params.toString()}`);
      if (!response.ok) {
        window.location.href = "/login";
        return;
      }
      const data = await response.json();
      document.getElementById("username").textContent = data.username;
      document.getElementById("email").textContent = data.email;
      document.getElementById("created").textContent =
        "Member since " + new Date(data.created_at).toLocaleDateString();
      currentPhotos = data.photos || [];
      renderGallery(currentPhotos);
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
            content_type: file.type || "application/octet-stream",
            taken_at: new Date(file.lastModified).toISOString(),
          }),
        });

        if (!uploadResponse.ok) {
          const payload = await uploadResponse.json().catch(() => ({}));
          status.textContent = `Failed ${file.name}: ${payload.error || "upload failed"}`;
          continue;
        }

        const savedPhoto = await uploadResponse.json();
        status.textContent = `${file.name}: complete`;
        await loadProfile();
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
    timestamp.textContent = `Uploaded: ${new Date(photo.created_at).toLocaleString()}`;

    const takenAt = document.createElement("p");
    takenAt.className = "mt-1 text-xs text-gray-400";
    takenAt.textContent = photo.taken_at
      ? `Taken: ${new Date(photo.taken_at).toLocaleString()}`
      : "Taken: Unknown";

    const actions = document.createElement("div");
    actions.className = "mt-3 flex gap-2";

    const downloadButton = document.createElement("button");
    downloadButton.className =
      "rounded-full border border-orange-300 bg-white px-3 py-1 text-xs font-medium text-gray-700";
    downloadButton.textContent = "Download";
    downloadButton.addEventListener("click", async () => {
      await downloadPhoto(photo.id);
    });

    const deleteButton = document.createElement("button");
    deleteButton.className =
      "rounded-full border border-red-300 bg-red-50 px-3 py-1 text-xs font-medium text-red-700";
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", async () => {
      await deletePhoto(photo.id, photo.filename);
    });

    actions.appendChild(downloadButton);
    actions.appendChild(deleteButton);

    card.appendChild(title);
    card.appendChild(description);
    card.appendChild(timestamp);
    card.appendChild(takenAt);
    card.appendChild(actions);
    return card;
  }

  async function downloadPhoto(photoId) {
    try {
      const response = await fetch(`/api/photos/download?photo_id=${photoId}`);
      const payload = await response.json();
      if (!response.ok) {
        alert(payload.error || "Download failed.");
        return;
      }
      const byteCharacters = atob(payload.image_base64 || "");
      const byteNumbers = new Array(byteCharacters.length);
      for (let i = 0; i < byteCharacters.length; i += 1) {
        byteNumbers[i] = byteCharacters.charCodeAt(i);
      }
      const blob = new Blob([new Uint8Array(byteNumbers)], {
        type: payload.content_type || "application/octet-stream",
      });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = payload.filename || `photo-${photoId}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (error) {
      alert(`Download failed: ${String(error)}`);
    }
  }

  async function deletePhoto(photoId, filename) {
    const approved = window.confirm(
      `Delete "${filename}" permanently? This cannot be undone.`,
    );
    if (!approved) {
      return;
    }
    try {
      const response = await fetch("/api/photos", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ photo_id: photoId, confirm_delete: true }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        alert(payload.error || "Delete failed.");
        return;
      }
      await loadProfile();
    } catch (error) {
      alert(`Delete failed: ${String(error)}`);
    }
  }
});
