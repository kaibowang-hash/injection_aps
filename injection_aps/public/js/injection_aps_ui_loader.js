frappe.provide("injection_aps.ui_loader");

(function () {
	if (injection_aps.ui_loader.__initialized) {
		return;
	}

	injection_aps.ui_loader.__initialized = true;
	injection_aps.ui_loader.__requests = {};

	injection_aps.ui_loader.load = function (expectedVersion) {
		const version = String(expectedVersion || "").trim();
		if (!version) {
			return Promise.reject(new Error("APS UI asset version is required."));
		}
		if (injection_aps.ui && injection_aps.ui.__asset_version === version) {
			injection_aps.ui.ensure_styles();
			return Promise.resolve(injection_aps.ui);
		}
		if (injection_aps.ui_loader.__requests[version]) {
			return injection_aps.ui_loader.__requests[version];
		}

		const request = new Promise((resolve, reject) => {
			const script = document.createElement("script");
			script.src = `/assets/injection_aps/js/injection_aps_shared.js?v=${encodeURIComponent(version)}`;
			script.async = true;
			script.dataset.injectionApsUiVersion = version;
			script.addEventListener("load", () => {
				if (!injection_aps.ui || injection_aps.ui.__asset_version !== version) {
					reject(new Error(`APS UI asset version mismatch: expected ${version}.`));
					return;
				}
				injection_aps.ui.ensure_styles();
				resolve(injection_aps.ui);
			}, { once: true });
			script.addEventListener("error", () => {
				reject(new Error(`Failed to load APS UI assets for version ${version}.`));
			}, { once: true });
			document.head.appendChild(script);
		});

		injection_aps.ui_loader.__requests[version] = request.catch((error) => {
			delete injection_aps.ui_loader.__requests[version];
			throw error;
		});
		return injection_aps.ui_loader.__requests[version];
	};

	injection_aps.ui_loader.start = function (expectedVersion, callback) {
		return injection_aps.ui_loader.load(expectedVersion)
			.then(() => callback && callback())
			.catch((error) => {
				console.error(error);
				frappe.msgprint({
					title: __("APS Interface Failed to Load", null, "Injection APS"),
					message: __("Refresh the page and try again. If the problem continues, contact the system administrator.", null, "Injection APS"),
					indicator: "red",
				});
			});
	};
})();
