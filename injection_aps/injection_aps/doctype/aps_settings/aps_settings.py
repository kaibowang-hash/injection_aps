from __future__ import annotations

from frappe.model.document import Document


class APSSettings(Document):
	def validate(self):
		self.planning_horizon_days = self.planning_horizon_days or 14
		self.release_horizon_days = self.release_horizon_days or 1
		self.freeze_days = self.freeze_days or 2
		self.default_setup_minutes = self.default_setup_minutes or 30
		self.default_first_article_minutes = self.default_first_article_minutes or 45
		self.default_hourly_capacity_qty = self.default_hourly_capacity_qty or 120
		self.item_food_grade_field = self.item_food_grade_field or "custom_food_grade"
		self.item_first_article_field = self.item_first_article_field or "custom_is_first_article"
		self.item_color_field = self.item_color_field or "color"
		self.item_material_field = self.item_material_field or "material"
		self.item_safety_stock_field = self.item_safety_stock_field or "safety_stock"
		self.item_max_stock_field = self.item_max_stock_field or "max_stock_qty"
		self.item_min_batch_field = self.item_min_batch_field or "min_order_qty"
		self.customer_short_name_field = self.customer_short_name_field or "custom_customer_short_name"
		self.workstation_risk_field = self.workstation_risk_field or "custom_production_risk_category"
		self.scheduling_item_risk_field = (
			self.scheduling_item_risk_field or "custom_workstation_risk_category_"
		)
		self.plant_floor_source_warehouse_field = (
			self.plant_floor_source_warehouse_field or "custom_default_source_warehouse"
		)
		self.plant_floor_wip_warehouse_field = self.plant_floor_wip_warehouse_field or "warehouse"
		self.plant_floor_fg_warehouse_field = (
			self.plant_floor_fg_warehouse_field or "custom_default_finished_goods_warehouse"
		)
		self.plant_floor_scrap_warehouse_field = (
			self.plant_floor_scrap_warehouse_field or "custom_default_scrap_warehouse"
		)
