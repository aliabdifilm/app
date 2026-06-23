document.addEventListener("DOMContentLoaded", () => {
    const uploadArea = document.getElementById("upload-area");
    const fileInput = document.getElementById("file-input");
    const editor = document.getElementById("editor");
    const originalImg = document.getElementById("original-img");
    const resultImg = document.getElementById("result-img");
    const compareOriginal = document.getElementById("compare-original");
    const compareResult = document.getElementById("compare-result");
    const compareView = document.getElementById("compare-view");
    const compareSlider = document.getElementById("compare-slider");
    const compareHandle = document.getElementById("compare-handle");
    const btnRetouch = document.getElementById("btn-retouch");
    const btnDownload = document.getElementById("btn-download");
    const btnNew = document.getElementById("btn-new");
    const overlay = document.getElementById("processing-overlay");
    const processingStatus = document.getElementById("processing-status");
    const tabs = document.querySelectorAll(".tab");

    let currentFilename = null;
    let resultFilename = null;

    // Upload handlers
    uploadArea.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", (e) => {
        if (e.target.files.length > 0) handleUpload(e.target.files[0]);
    });

    uploadArea.addEventListener("dragover", (e) => {
        e.preventDefault();
        uploadArea.classList.add("dragover");
    });
    uploadArea.addEventListener("dragleave", () => {
        uploadArea.classList.remove("dragover");
    });
    uploadArea.addEventListener("drop", (e) => {
        e.preventDefault();
        uploadArea.classList.remove("dragover");
        if (e.dataTransfer.files.length > 0) handleUpload(e.dataTransfer.files[0]);
    });

    async function handleUpload(file) {
        const formData = new FormData();
        formData.append("photo", file);

        try {
            const resp = await fetch("/upload", { method: "POST", body: formData });
            const data = await resp.json();

            if (data.success) {
                currentFilename = data.filename;
                originalImg.src = data.url;
                compareOriginal.src = data.url;
                uploadArea.classList.add("hidden");
                editor.classList.remove("hidden");
                resultImg.classList.add("hidden");
                compareView.classList.add("hidden");
                btnDownload.classList.add("hidden");
                resultFilename = null;
                activateTab("original");
            } else {
                alert(data.error || "Upload failed");
            }
        } catch (err) {
            alert("Upload failed: " + err.message);
        }
    }

    // Tab switching
    tabs.forEach((tab) => {
        tab.addEventListener("click", () => activateTab(tab.dataset.tab));
    });

    function activateTab(name) {
        tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
        originalImg.classList.toggle("hidden", name !== "original");
        resultImg.classList.toggle("hidden", name !== "result");
        compareView.classList.toggle("hidden", name !== "compare");
    }

    // Compare slider
    let isDragging = false;
    compareHandle.addEventListener("mousedown", () => (isDragging = true));
    document.addEventListener("mouseup", () => (isDragging = false));
    document.addEventListener("mousemove", (e) => {
        if (!isDragging) return;
        const rect = compareView.getBoundingClientRect();
        let x = (e.clientX - rect.left) / rect.width;
        x = Math.max(0.05, Math.min(0.95, x));
        compareSlider.style.width = (x * 100) + "%";
        compareHandle.style.left = (x * 100) + "%";
    });

    // Touch support for compare
    compareHandle.addEventListener("touchstart", () => (isDragging = true));
    document.addEventListener("touchend", () => (isDragging = false));
    document.addEventListener("touchmove", (e) => {
        if (!isDragging) return;
        const rect = compareView.getBoundingClientRect();
        let x = (e.touches[0].clientX - rect.left) / rect.width;
        x = Math.max(0.05, Math.min(0.95, x));
        compareSlider.style.width = (x * 100) + "%";
        compareHandle.style.left = (x * 100) + "%";
    });

    // Sliders: update value display
    document.querySelectorAll('.setting input[type="range"]').forEach((slider) => {
        const valueDisplay = slider.parentElement.querySelector(".value");
        slider.addEventListener("input", () => {
            valueDisplay.textContent = slider.value + "%";
        });
    });

    // Retouch button
    btnRetouch.addEventListener("click", async () => {
        if (!currentFilename) return;

        const settings = {};
        document.querySelectorAll('.setting input[type="range"]').forEach((slider) => {
            settings[slider.dataset.key] = parseInt(slider.value) / 100;
        });

        overlay.classList.remove("hidden");
        processingStatus.textContent = "Starting retouching pipeline...";
        btnRetouch.disabled = true;
        const btnText = btnRetouch.querySelector(".btn-text");
        const btnLoading = btnRetouch.querySelector(".btn-loading");
        btnText.classList.add("hidden");
        btnLoading.classList.remove("hidden");

        try {
            const resp = await fetch("/retouch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ filename: currentFilename, settings }),
            });
            const data = await resp.json();

            if (data.success) {
                resultImg.src = data.url;
                compareResult.src = data.url;
                resultFilename = data.filename;
                btnDownload.classList.remove("hidden");
                activateTab("result");
                processingStatus.textContent =
                    `Done in ${data.processing_time}s`;
            } else {
                alert(data.error || "Processing failed");
            }
        } catch (err) {
            alert("Processing failed: " + err.message);
        } finally {
            overlay.classList.add("hidden");
            btnRetouch.disabled = false;
            btnText.classList.remove("hidden");
            btnLoading.classList.add("hidden");
        }
    });

    // Download
    btnDownload.addEventListener("click", () => {
        if (resultFilename) {
            window.location.href = "/download/" + resultFilename;
        }
    });

    // New photo
    btnNew.addEventListener("click", () => {
        editor.classList.add("hidden");
        uploadArea.classList.remove("hidden");
        fileInput.value = "";
        currentFilename = null;
        resultFilename = null;
    });
});
