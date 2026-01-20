"""
SMS Service for sending notifications via Twilio.
"""
from typing import Optional
import logging
from twilio.rest import Client
from twilio.base.exceptions import TwilioException

from app.core.config import settings

logger = logging.getLogger(__name__)


class SMSService:
    """
    Service for sending SMS notifications via Twilio.
    """

    def __init__(self):
        """Initialize Twilio client with credentials from settings."""
        self.enabled = settings.twilio_enabled and settings.twilio_sms_enabled
        self.client = None
        
        if self.enabled and settings.twilio_account_sid and settings.twilio_auth_token:
            try:
                # Initialize with timeout to prevent hanging requests
                self.client = Client(
                    settings.twilio_account_sid,
                    settings.twilio_auth_token,
                    timeout=10  # 10 second timeout for Twilio API calls
                )
                logger.info("Twilio SMS client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Twilio client: {e}")
                self.enabled = False
        else:
            logger.warning("Twilio SMS is disabled or credentials are missing")
            self.enabled = False

    def format_phone_number(self, phone_number: str) -> str:
        """
        Format phone number to E.164 format for Twilio.
        
        Args:
            phone_number: Phone number in any format
            
        Returns:
            Formatted phone number in E.164 format (e.g., +1234567890)
        """
        if not phone_number:
            return ""
        
        # Remove all non-digit characters (including spaces)
        digits = ''.join(filter(str.isdigit, phone_number))
        
        # If it starts with 0, remove it (common in Indian numbers)
        if digits.startswith('0'):
            digits = digits[1:]
        
        # If it doesn't start with country code, assume it's Indian (+91)
        if len(digits) == 10:
            formatted = f"+91{digits}"
        elif len(digits) == 12 and digits.startswith('91'):
            formatted = f"+{digits}"
        elif phone_number.startswith('+'):
            # Already has country code, just clean it
            formatted = f"+{digits}" if not digits.startswith('+') else digits
        else:
            formatted = f"+{digits}"
        
        logger.info(f"Phone number formatting: '{phone_number}' -> '{formatted}' (digits: {digits}, length: {len(digits)})")
        return formatted

    def send_visitor_notification(
        self,
        to_phone: str,
        visitor_name: str,
        visitor_mobile: str,
        visitor_email: Optional[str],
        visitor_company: Optional[str],
        reason_for_visit: str,
        visitor_id: str,
        warehouse: Optional[str] = None,
        person_to_meet_name: Optional[str] = None,
        date_of_visit: Optional[str] = None,
        time_slot: Optional[str] = None,
    ) -> bool:
        """
        Send SMS notification to approver about new visitor check-in.
        
        Args:
            to_phone: Phone number of the person to notify (from vis_approvers.ph_no)
            visitor_name: Name of the visitor
            visitor_mobile: Mobile number of the visitor
            visitor_email: Email of the visitor (optional)
            visitor_company: Company name of the visitor (optional)
            reason_for_visit: Reason for the visit
            visitor_id: Visitor ID (YYYYMMDDHHMMSS format)
            warehouse: Warehouse location (optional)
            
        Returns:
            True if SMS was sent successfully, False otherwise
        """
        if not self.enabled:
            logger.warning(f"SMS service is disabled. twilio_enabled={settings.twilio_enabled}, twilio_sms_enabled={settings.twilio_sms_enabled}")
            return False

        if not self.client:
            logger.warning("Twilio client not initialized. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
            return False

        if not to_phone:
            logger.warning("No phone number provided for SMS notification")
            return False

        try:
            # Format phone number
            formatted_to = self.format_phone_number(to_phone)
            
            # Determine sender ID/number (priority: custom sender ID > custom phone > Twilio number)
            from_number = None
            if settings.twilio_custom_sender_id:
                # Use Alphanumeric Sender ID (e.g., "CANDOR", "COMPANY")
                from_number = settings.twilio_custom_sender_id
                logger.info(f"Using custom sender ID: {from_number}")
            elif settings.twilio_custom_phone_number:
                # Use custom verified phone number
                from_number = self.format_phone_number(settings.twilio_custom_phone_number)
                logger.info(f"Using custom phone number: {from_number}")
            elif settings.twilio_phone_number:
                # Fallback to Twilio purchased number
                from_number = settings.twilio_phone_number
                logger.info(f"Using Twilio phone number: {from_number}")
            else:
                logger.error("No sender number/ID configured. Set TWILIO_CUSTOM_SENDER_ID, TWILIO_CUSTOM_PHONE_NUMBER, or TWILIO_PHONE_NUMBER")
                return False

            # Build SMS message
            is_appointment = reason_for_visit.startswith("[APPOINTMENT]")
            message_header = "🔔 New Appointment Request" if is_appointment else "🔔 New Visitor Check-In"
            
            message_parts = [
                message_header,
                "",
                f"Visitor Name: {visitor_name}",
                f"Mobile: {visitor_mobile}",
            ]
            
            if visitor_email:
                message_parts.append(f"Email: {visitor_email}")
            
            if visitor_company:
                message_parts.append(f"Company: {visitor_company}")
            
            if person_to_meet_name:
                message_parts.append(f"Coming to Meet: {person_to_meet_name}")
            
            # Add appointment details
            if is_appointment:
                message_parts.append("")
                message_parts.append("📅 Appointment Details:")
                if date_of_visit:
                    message_parts.append(f"Date: {date_of_visit}")
                if time_slot:
                    message_parts.append(f"Time: {time_slot}")
                # Extract purpose from reason (remove [APPOINTMENT] prefix)
                purpose = reason_for_visit.replace("[APPOINTMENT] ", "").strip()
                message_parts.append(f"Purpose: {purpose}")
            else:
                # For regular check-ins, show reason
                message_parts.append(f"Reason: {reason_for_visit}")
            
            message_parts.append(f"Visitor ID: {visitor_id}")
            
            if warehouse:
                message_parts.append(f"Warehouse: {warehouse}")
            
            # Get dashboard URL for SMS link
            # Use dashboard_url if available, otherwise append /dashboard to frontend_url
            if hasattr(settings, 'dashboard_url') and settings.dashboard_url:
                dashboard_url = settings.dashboard_url.rstrip('/')
            else:
                dashboard_url = f"{settings.frontend_url.rstrip('/')}/dashboard"
            
            message_parts.extend([
                "",
                "Please review and approve/reject the visitor request.",
                "",
                f"Click here to view dashboard:",
                f"{dashboard_url}",
                "",
                "Login with your credentials if not already logged in.",
            ])
            
            message_body = "\n".join(message_parts)

            # Send SMS
            logger.info(f"Sending SMS from {from_number} to {formatted_to} about visitor {visitor_id}")
            
            # Use Messaging Service if configured (allows verified numbers to work)
            if settings.twilio_messaging_service_sid:
                message = self.client.messages.create(
                    body=message_body,
                    messaging_service_sid=settings.twilio_messaging_service_sid,
                    to=formatted_to
                )
                logger.info(f"Using Messaging Service: {settings.twilio_messaging_service_sid}")
            else:
                message = self.client.messages.create(
                    body=message_body,
                    from_=from_number,
                    to=formatted_to
                )
            
            logger.info(f"SMS sent successfully. SID: {message.sid}")
            logger.info(f"SMS Status: {message.status}, Error Code: {message.error_code}, Error Message: {message.error_message}")
            
            # Check for delivery issues
            if message.status == 'failed' or message.error_code:
                logger.error(f"SMS delivery failed! Status: {message.status}, Error Code: {message.error_code}, Error Message: {message.error_message}")
                logger.error(f"Common issues:")
                logger.error(f"  - Trial account: Can only send to verified numbers")
                logger.error(f"  - Invalid phone number format")
                logger.error(f"  - Carrier blocking")
                logger.error(f"  - Insufficient Twilio balance")
                return False
            
            # Log message details for debugging
            logger.info(f"Message Details - To: {formatted_to}, From: {from_number}, Status: {message.status}, Price: {message.price}, Price Unit: {message.price_unit}")
            
            return True

        except TwilioException as e:
            logger.error(f"Twilio error sending SMS: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending SMS: {e}")
            return False

    def send_approval_notification(
        self,
        to_phone: str,
        visitor_name: str,
        person_to_meet_name: Optional[str] = None,
        visitor_id: Optional[str] = None,
        is_appointment: bool = False,
        appointment_date: Optional[str] = None,
        appointment_time: Optional[str] = None,
    ) -> bool:
        """
        Send SMS notification to visitor when their request is approved.
        
        Args:
            to_phone: Phone number of the visitor
            visitor_name: Name of the visitor
            person_to_meet_name: Name of the person they're meeting (optional)
            visitor_id: Visitor ID (optional)
            is_appointment: Whether this is an appointment (optional)
            appointment_date: Appointment date (optional)
            appointment_time: Appointment time (optional)
            
        Returns:
            True if SMS was sent successfully, False otherwise
        """
        if not self.enabled:
            logger.warning(f"SMS service is disabled. twilio_enabled={settings.twilio_enabled}, twilio_sms_enabled={settings.twilio_sms_enabled}")
            return False

        if not self.client:
            logger.warning("Twilio client not initialized. Check TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
            return False

        if not to_phone:
            logger.warning("No phone number provided for approval SMS notification")
            return False

        try:
            # Format phone number
            formatted_to = self.format_phone_number(to_phone)
            
            # Determine sender ID/number
            from_number = None
            if settings.twilio_custom_sender_id:
                from_number = settings.twilio_custom_sender_id
                logger.info(f"Using custom sender ID: {from_number}")
            elif settings.twilio_custom_phone_number:
                from_number = self.format_phone_number(settings.twilio_custom_phone_number)
                logger.info(f"Using custom phone number: {from_number}")
            elif settings.twilio_phone_number:
                from_number = settings.twilio_phone_number
                logger.info(f"Using Twilio phone number: {from_number}")
            else:
                logger.error("No sender number/ID configured. Set TWILIO_CUSTOM_SENDER_ID, TWILIO_CUSTOM_PHONE_NUMBER, or TWILIO_PHONE_NUMBER")
                return False

            # Build SMS message
            message_parts = [
                "✅ Your visit request has been approved!",
                "",
                f"Dear {visitor_name},",
                "",
            ]
            
            if is_appointment:
                message_parts.append("Your appointment request has been approved. Please come and visit us.")
                if appointment_date:
                    message_parts.append(f"📅 Date: {appointment_date}")
                if appointment_time:
                    message_parts.append(f"🕐 Time: {appointment_time}")
            else:
                message_parts.append("Your visit request has been approved. Please come and visit us at your convenience.")
            
            if person_to_meet_name:
                message_parts.append(f"👤 Meeting with: {person_to_meet_name}")
            
            if visitor_id:
                message_parts.append(f"🆔 Visitor ID: {visitor_id}")
            
            message_parts.extend([
                "",
                "We look forward to seeing you!",
                "",
                "Thank you,",
                "Candor Foods"
            ])
            
            message_body = "\n".join(message_parts)

            # Send SMS
            logger.info(f"Sending approval SMS from {from_number} to {formatted_to} for visitor {visitor_name}")
            
            # Use Messaging Service if configured
            if settings.twilio_messaging_service_sid:
                message = self.client.messages.create(
                    body=message_body,
                    messaging_service_sid=settings.twilio_messaging_service_sid,
                    to=formatted_to
                )
                logger.info(f"Using Messaging Service: {settings.twilio_messaging_service_sid}")
            else:
                message = self.client.messages.create(
                    body=message_body,
                    from_=from_number,
                    to=formatted_to
                )
            
            logger.info(f"Approval SMS sent successfully. SID: {message.sid}")
            logger.info(f"SMS Status: {message.status}, Error Code: {message.error_code}, Error Message: {message.error_message}")
            
            # Check for delivery issues
            if message.status == 'failed' or message.error_code:
                logger.error(f"Approval SMS delivery failed! Status: {message.status}, Error Code: {message.error_code}, Error Message: {message.error_message}")
                return False
            
            return True

        except TwilioException as e:
            logger.error(f"Twilio error sending approval SMS: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending approval SMS: {e}")
            return False


# Create a singleton instance
sms_service = SMSService()

