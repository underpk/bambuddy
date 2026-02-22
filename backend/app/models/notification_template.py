"""Notification template model for customizable notification messages."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class NotificationTemplate(Base):
    """Model for notification message templates."""

    __tablename__ = "notification_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    title_template: Mapped[str] = mapped_column(Text, nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())


_SEP = "\u2501" * 17  # ━━━━━━━━━━━━━━━━━

# Old default templates for migration detection.
# If a user's DB template matches these old values, it's safe to auto-upgrade.
OLD_DEFAULT_TEMPLATES: dict[str, dict[str, str]] = {
    "print_start": {
        "title_template": "Print Started",
        "body_template": "{printer}: {filename}\nEstimated: {estimated_time}",
    },
    "print_complete": {
        "title_template": "Print Completed",
        "body_template": "{printer}: {filename}\nTime: {duration}\nFilament: {filament_grams}g",
    },
    "print_failed": {
        "title_template": "Print Failed",
        "body_template": "{printer}: {filename}\nTime: {duration}\nReason: {reason}",
    },
    "print_stopped": {
        "title_template": "Print Stopped",
        "body_template": "{printer}: {filename}\nTime: {duration}",
    },
    "print_progress": {
        "title_template": "Print {progress}% Complete",
        "body_template": "{printer}: {filename}\nRemaining: {remaining_time}",
    },
    "printer_offline": {
        "title_template": "Printer Offline",
        "body_template": "{printer} has disconnected",
    },
    "printer_error": {
        "title_template": "Printer Error: {error_type}",
        "body_template": "{printer}\n{error_detail}",
    },
    "plate_not_empty": {
        "title_template": "Plate Not Empty - Print Paused",
        "body_template": "{printer}: Objects detected on build plate. Print has been paused. Clear plate and resume.",
    },
    "filament_low": {
        "title_template": "Filament Low",
        "body_template": "{printer}: Slot {slot} at {remaining_percent}%",
    },
    "maintenance_due": {
        "title_template": "Maintenance Due",
        "body_template": "{printer}:\n{items}",
    },
    "ams_humidity_high": {
        "title_template": "AMS Humidity Alert",
        "body_template": "{printer} {ams_label}: Humidity {humidity}% exceeds {threshold}% threshold",
    },
    "ams_temperature_high": {
        "title_template": "AMS Temperature Alert",
        "body_template": "{printer} {ams_label}: Temperature {temperature}°C exceeds {threshold}°C threshold",
    },
    "bed_cooled": {
        "title_template": "Bed Cooled",
        "body_template": "{printer}: Bed cooled to {bed_temp}°C (threshold: {threshold}°C)",
    },
    "test": {
        "title_template": "Bambuddy Test",
        "body_template": "This is a test notification. If you see this, notifications are working!",
    },
    "queue_job_added": {
        "title_template": "Job Queued",
        "body_template": "{job_name} added to queue for {target}",
    },
    "queue_job_assigned": {
        "title_template": "Job Assigned",
        "body_template": "{job_name} assigned to {printer} (from Any {target_model} queue)",
    },
    "queue_job_started": {
        "title_template": "Queue Job Started",
        "body_template": "{printer}: {job_name}\nEstimated: {estimated_time}",
    },
    "queue_job_waiting": {
        "title_template": "Job Waiting for Filament",
        "body_template": "{job_name} waiting for {target_model}\n{waiting_reason}",
    },
    "queue_job_skipped": {
        "title_template": "Job Skipped",
        "body_template": "{printer}: {job_name}\nReason: {reason}",
    },
    "queue_job_failed": {
        "title_template": "Job Failed to Start",
        "body_template": "{printer}: {job_name}\nReason: {reason}",
    },
    "queue_completed": {
        "title_template": "Queue Complete",
        "body_template": "All {completed_count} queued jobs have finished",
    },
}

# Default templates for seeding
DEFAULT_TEMPLATES = [
    {
        "event_type": "print_start",
        "name": "Print Started",
        "title_template": f"\U0001f680 Print Started",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{filename}}\n\u23f1 Est. Time: {{estimated_time}}",
    },
    {
        "event_type": "print_complete",
        "name": "Print Completed",
        "title_template": f"\u2705 Print Completed",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{filename}}\n\u23f1 Time: {{duration}}\n\U0001f9f5 Filament: {{filament_grams}}g",
    },
    {
        "event_type": "print_failed",
        "name": "Print Failed",
        "title_template": f"\u274c Print Failed",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{filename}}\n\u23f1 Time: {{duration}}\n\u26a0 Reason: {{reason}}",
    },
    {
        "event_type": "print_stopped",
        "name": "Print Stopped",
        "title_template": f"\u23f9 Print Stopped",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{filename}}\n\u23f1 Time: {{duration}}",
    },
    {
        "event_type": "print_progress",
        "name": "Print Progress",
        "title_template": f"\U0001f4ca Print {{progress}}% Complete",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{filename}}\n\u23f1 Remaining: {{remaining_time}}",
    },
    {
        "event_type": "printer_offline",
        "name": "Printer Offline",
        "title_template": f"\U0001f534 Printer Offline",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}} has disconnected",
    },
    {
        "event_type": "printer_error",
        "name": "Printer Error",
        "title_template": f"\U0001f6a8 Printer Error: {{error_type}}",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\u26a0 {{error_detail}}",
    },
    {
        "event_type": "plate_not_empty",
        "name": "Plate Not Empty",
        "title_template": f"\U0001f37d Plate Not Empty - Print Paused",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\u26a0 Objects detected on build plate. Print has been paused. Clear plate and resume.",
    },
    {
        "event_type": "filament_low",
        "name": "Filament Low",
        "title_template": f"\U0001f9f5 Filament Low",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\u26a0 Slot {{slot}} at {{remaining_percent}}%",
    },
    {
        "event_type": "maintenance_due",
        "name": "Maintenance Due",
        "title_template": f"\U0001f527 Maintenance Due",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n{{items}}",
    },
    {
        "event_type": "ams_humidity_high",
        "name": "AMS Humidity High",
        "title_template": f"\U0001f4a7 AMS Humidity Alert",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}} {{ams_label}}\n\u26a0 Humidity {{humidity}}% exceeds {{threshold}}% threshold",
    },
    {
        "event_type": "ams_temperature_high",
        "name": "AMS Temperature High",
        "title_template": f"\U0001f321 AMS Temperature Alert",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}} {{ams_label}}\n\u26a0 Temperature {{temperature}}\u00b0C exceeds {{threshold}}\u00b0C threshold",
    },
    {
        "event_type": "bed_cooled",
        "name": "Bed Cooled",
        "title_template": f"\u2744 Bed Cooled",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f321 Bed cooled to {{bed_temp}}\u00b0C (threshold: {{threshold}}\u00b0C)",
    },
    {
        "event_type": "test",
        "name": "Test Notification",
        "title_template": f"\U0001f514 Bambuddy Test",
        "body_template": f"{_SEP}\nThis is a test notification. If you see this, notifications are working!",
    },
    # Queue notifications
    {
        "event_type": "queue_job_added",
        "name": "Queue Job Added",
        "title_template": f"\U0001f4cb Job Queued",
        "body_template": f"{_SEP}\n\U0001f4c4 {{job_name}}\n\U0001f5a8 Added to queue for {{target}}",
    },
    {
        "event_type": "queue_job_assigned",
        "name": "Queue Job Assigned",
        "title_template": f"\U0001f4cb Job Assigned",
        "body_template": f"{_SEP}\n\U0001f4c4 {{job_name}}\n\U0001f5a8 Assigned to {{printer}} (from Any {{target_model}} queue)",
    },
    {
        "event_type": "queue_job_started",
        "name": "Queue Job Started",
        "title_template": f"\U0001f4cb Queue Job Started",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{job_name}}\n\u23f1 Est. Time: {{estimated_time}}",
    },
    {
        "event_type": "queue_job_waiting",
        "name": "Queue Job Waiting",
        "title_template": f"\U0001f4cb Job Waiting for Filament",
        "body_template": f"{_SEP}\n\U0001f4c4 {{job_name}}\n\U0001f5a8 Waiting for {{target_model}}\n\u26a0 {{waiting_reason}}",
    },
    {
        "event_type": "queue_job_skipped",
        "name": "Queue Job Skipped",
        "title_template": f"\U0001f4cb Job Skipped",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{job_name}}\n\u26a0 Reason: {{reason}}",
    },
    {
        "event_type": "queue_job_failed",
        "name": "Queue Job Failed",
        "title_template": f"\U0001f4cb Job Failed to Start",
        "body_template": f"{_SEP}\n\U0001f5a8 {{printer}}\n\U0001f4c4 {{job_name}}\n\u26a0 Reason: {{reason}}",
    },
    {
        "event_type": "queue_completed",
        "name": "Queue Completed",
        "title_template": f"\U0001f4cb Queue Complete",
        "body_template": f"{_SEP}\n\u2705 All {{completed_count}} queued jobs have finished",
    },
    {
        "event_type": "user_created",
        "name": "Welcome Email",
        "title_template": "Welcome to {app_name}",
        "body_template": "Welcome {username}!\n\nYour account has been created.\nUsername: {username}\nPassword: {password}\n\nLogin at: {login_url}",
    },
    {
        "event_type": "password_reset",
        "name": "Password Reset",
        "title_template": "{app_name} - Password Reset",
        "body_template": "Hello {username},\n\nYour password has been reset.\nNew Password: {password}\n\nLogin at: {login_url}",
    },
]
