"""
SMS Webhook Router for handling Twilio SMS replies.
Allows approvers to approve/reject visitors via SMS reply.
"""
from fastapi import APIRouter, Request, HTTPException, Form, status, Depends
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime
import logging
import re

from app.core.database import get_db
from app.models.visitor import Visitor, VisitorStatus
from app.models.approver import Approver
from app.services.sms_service import sms_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sms", tags=["SMS Webhook"])

# In-memory cache to track pending rejections: {approver_phone: visitor_id}
# This tracks when an approver has sent "REJECT" and we're waiting for the reason
pending_rejections = {}


@router.get("/webhook", status_code=status.HTTP_200_OK)
async def webhook_health_check():
    """
    Health check endpoint for Twilio webhook.
    Twilio may ping this endpoint to verify it's accessible.
    """
    return {
        "status": "ok",
        "message": "SMS webhook endpoint is ready",
        "endpoint": "/api/sms/webhook"
    }


def format_phone_number(phone_number: str) -> str:
    """Format phone number to E.164 format."""
    digits = ''.join(filter(str.isdigit, phone_number))
    
    if digits.startswith('0'):
        digits = digits[1:]
    
    if len(digits) == 10:
        return f"+91{digits}"
    elif len(digits) == 12 and digits.startswith('91'):
        return f"+{digits}"
    elif phone_number.startswith('+'):
        return phone_number
    else:
        return f"+{digits}"


def normalize_phone_for_matching(phone: str) -> str:
    """Normalize phone number for database matching - returns last 10 digits."""
    digits = ''.join(filter(str.isdigit, phone))
    if len(digits) >= 10:
        return digits[-10:]  # Return last 10 digits
    return digits


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def handle_sms_webhook(
    request: Request,
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Handle incoming SMS replies from Twilio.
    
    When an approver replies to the SMS notification, Twilio sends a webhook here.
    The approver can reply with:
    - "APPROVED" or "APPROVE" - to approve the visitor
    - "REJECTED" or "REJECT" - to reject the visitor
    - "APPROVED [VISITOR_ID]" - to approve a specific visitor
    - "REJECTED [VISITOR_ID]" - to reject a specific visitor
    
    Args:
        From: Phone number that sent the SMS (approver's number)
        To: Phone number that received the SMS (Twilio number)
        Body: SMS message content
        db: Database session
        
    Returns:
        TwiML response (XML) to send a confirmation SMS
    """
    try:
        # Format the sender's phone number
        approver_phone = format_phone_number(From)
        # Keep original message for logging, but use uppercase for matching (case-insensitive)
        message_body_original = Body.strip()
        message_body = message_body_original.upper()
        
        logger.info("=" * 60)
        logger.info(f"WEBHOOK: Received SMS from {approver_phone}: {message_body_original} (normalized: {message_body})")
        logger.info(f"WEBHOOK: To number: {To}")
        print(f"[WEBHOOK] Received SMS from {approver_phone}: '{message_body_original}' (case-insensitive matching)")
        
        # Find approver by phone number - try multiple matching strategies
        normalized_phone = normalize_phone_for_matching(approver_phone)
        logger.info(f"WEBHOOK: Normalized phone for matching: {normalized_phone} (from {approver_phone})")
        print(f"[WEBHOOK] Normalized phone: {normalized_phone}")
        
        # Try multiple matching strategies
        approver = None
        
        # Strategy 1: Match last 10 digits in database
        approver = db.query(Approver).filter(
            Approver.ph_no.like(f"%{normalized_phone}%")
        ).first()
        
        if approver:
            logger.info(f"WEBHOOK: Found approver via LIKE match: {approver.username} (phone in DB: {approver.ph_no})")
            print(f"[WEBHOOK] Found approver via LIKE: {approver.username}")
        else:
            # Strategy 2: Try exact match
            approver = db.query(Approver).filter(Approver.ph_no == approver_phone).first()
            if approver:
                logger.info(f"WEBHOOK: Found approver via exact match: {approver.username}")
                print(f"[WEBHOOK] Found approver via exact match: {approver.username}")
            else:
                # Strategy 3: Try matching normalized phone in database
                all_approvers = db.query(Approver).all()
                for app in all_approvers:
                    if app.ph_no:
                        db_normalized = normalize_phone_for_matching(app.ph_no)
                        if db_normalized == normalized_phone:
                            approver = app
                            logger.info(f"WEBHOOK: Found approver via normalized match: {approver.username} (DB: {app.ph_no})")
                            print(f"[WEBHOOK] Found approver via normalized match: {approver.username}")
                            break
        
        if not approver:
            logger.warning(f"WEBHOOK: No approver found with phone number: {approver_phone} (normalized: {normalized_phone})")
            logger.warning(f"WEBHOOK: Available approvers in DB:")
            all_approvers = db.query(Approver).all()
            for app in all_approvers:
                logger.warning(f"  - {app.username}: {app.ph_no} (normalized: {normalize_phone_for_matching(app.ph_no) if app.ph_no else 'N/A'})")
            print(f"[WEBHOOK] ERROR: No approver found for phone {approver_phone}")
            return _twiml_response("Sorry, your phone number is not registered. Please contact admin.")
        
        # Parse the message to extract action and visitor ID
        action = None
        visitor_id = None
        
        # Check for explicit visitor ID in message (e.g., "APPROVED 20260102090655")
        visitor_id_match = re.search(r'\d{14}', message_body)
        if visitor_id_match:
            visitor_id = visitor_id_match.group(0)
            # Remove visitor ID from message to get action
            message_without_id = re.sub(r'\d{14}', '', message_body).strip()
        else:
            message_without_id = message_body
        
        # Check if there's a pending rejection for this approver (waiting for reason)
        pending_visitor_id = pending_rejections.get(approver_phone)
        
        # Determine action (check for approve keywords first, then reject)
        # Case-insensitive matching - handles: Approve, approve, APPROVE, appro, etc.
        # Approve keywords: APPROVED, APPROVE, YES, OK, and partial matches like "appro"
        # Reject keywords: REJECTED, REJECT, NO, DENY, and partial matches like "rej"
        message_upper = message_without_id.upper().strip()
        
        # Check for approve keywords (case-insensitive, including partial matches)
        approve_keywords = ['APPROVED', 'APPROVE', 'YES', 'OK', 'Y', 'APPRO', 'APROV']
        reject_keywords = ['REJECTED', 'REJECT', 'NO', 'DENY', 'N', 'REJ']
        
        # If there's a pending rejection, treat this message as the rejection reason
        if pending_visitor_id:
            logger.info(f"WEBHOOK: Pending rejection found for visitor {pending_visitor_id}, treating message as reason")
            print(f"[WEBHOOK] Pending rejection for visitor {pending_visitor_id}, message is rejection reason")
            
            # Find the visitor
            try:
                visitor_id_int = int(pending_visitor_id)
                visitor = db.query(Visitor).filter(
                    Visitor.id == visitor_id_int,
                    (Visitor.person_to_meet == approver.username) | (Visitor.person_to_meet == approver.name),
                    Visitor.status == VisitorStatus.WAITING
                ).first()
                
                if visitor:
                    # Use the message as rejection reason
                    rejection_reason = message_body_original.strip()
                    visitor.status = VisitorStatus.REJECTED
                    visitor.rejection_reason = rejection_reason
                    
                    try:
                        db.commit()
                        db.refresh(visitor)
                        # Clear pending rejection
                        pending_rejections.pop(approver_phone, None)
                        
                        logger.info(f"WEBHOOK: Visitor {visitor.id} rejected with reason: {rejection_reason}")
                        print(f"[WEBHOOK] SUCCESS: Visitor {visitor.id} rejected with reason")
                        
                        return _twiml_response(
                            f"Visitor {visitor.id} has been rejected.\n"
                            f"Reason: {rejection_reason}\n"
                            f"Status: REJECTED"
                        )
                    except Exception as e:
                        db.rollback()
                        logger.error(f"WEBHOOK: Failed to reject visitor: {e}", exc_info=True)
                        print(f"[WEBHOOK] ERROR: Failed to reject visitor: {e}")
                        return _twiml_response("Error updating visitor status. Please try again.")
                else:
                    # Visitor not found or already processed, clear pending rejection
                    pending_rejections.pop(approver_phone, None)
                    logger.warning(f"WEBHOOK: Pending visitor {pending_visitor_id} not found or already processed")
                    return _twiml_response("Visitor not found or already processed. Please start over.")
            except ValueError:
                pending_rejections.pop(approver_phone, None)
                return _twiml_response("Invalid visitor ID. Please start over.")
        
        # Check if any approve keyword is in the message (partial match allowed)
        if any(keyword in message_upper for keyword in approve_keywords):
            # Clear any pending rejection if approving
            pending_rejections.pop(approver_phone, None)
            action = 'APPROVED'
            logger.info(f"WEBHOOK: Matched approve keyword in message: {message_upper}")
            print(f"[WEBHOOK] Matched approve keyword: {message_upper}")
        # Check if any reject keyword is in the message (partial match allowed)
        elif any(keyword in message_upper for keyword in reject_keywords):
            action = 'REJECT_INITIATED'  # Special action to ask for reason
            logger.info(f"WEBHOOK: Matched reject keyword in message: {message_upper}")
            print(f"[WEBHOOK] Matched reject keyword: {message_upper}")
        else:
            logger.warning(f"WEBHOOK: No valid keyword found in message: {message_upper}")
            print(f"[WEBHOOK] Invalid message: {message_upper}")
            return _twiml_response(
                "Invalid reply. Reply with:\n"
                "- APPROVED, APPROVE, YES, OK, or Y (to approve)\n"
                "- REJECT, REJECTED, NO, DENY, or N (to reject - you'll be asked for reason)\n"
                "Or include visitor ID: APPROVED 20260102090655\n"
                "(Case-insensitive - 'approve', 'Appro', 'REJECT', etc. all work)"
            )
        
        # Find the visitor to update
        visitor = None
        
        logger.info(f"WEBHOOK: Looking for visitor - Approver: {approver.username} (name: {approver.name})")
        print(f"[WEBHOOK] Approver found: {approver.username}, Phone: {approver.ph_no}")
        
        if visitor_id:
            # Update specific visitor by ID
            try:
                visitor_id_int = int(visitor_id)
                logger.info(f"WEBHOOK: Searching for specific visitor ID: {visitor_id_int}")
                print(f"[WEBHOOK] Searching for visitor ID: {visitor_id_int}")
                visitor = db.query(Visitor).filter(
                    Visitor.id == visitor_id_int,
                    (Visitor.person_to_meet == approver.username) | (Visitor.person_to_meet == approver.name)
                ).first()
                if visitor:
                    logger.info(f"WEBHOOK: Found visitor {visitor.id} with status: {visitor.status}")
                    print(f"[WEBHOOK] Found visitor {visitor.id}, current status: {visitor.status}")
                else:
                    logger.warning(f"WEBHOOK: Visitor {visitor_id_int} not found or not assigned to {approver.username}")
                    print(f"[WEBHOOK] Visitor {visitor_id_int} not found")
            except ValueError as e:
                logger.error(f"WEBHOOK: Invalid visitor ID format: {visitor_id} - {e}")
                print(f"[WEBHOOK] Invalid visitor ID: {visitor_id}")
        else:
            # Find the most recent pending visitor for this approver
            logger.info(f"WEBHOOK: Searching for most recent WAITING visitor for {approver.username} (name: {approver.name})")
            print(f"[WEBHOOK] Searching for most recent WAITING visitor for {approver.username}")
            
            # Count total WAITING visitors for this approver
            waiting_count = db.query(Visitor).filter(
                (Visitor.person_to_meet == approver.username) | (Visitor.person_to_meet == approver.name),
                Visitor.status == VisitorStatus.WAITING
            ).count()
            
            logger.info(f"WEBHOOK: Found {waiting_count} WAITING visitors for {approver.username}")
            print(f"[WEBHOOK] Total WAITING visitors: {waiting_count}")
            
            # Try to find visitor by username first
            visitor = db.query(Visitor).filter(
                Visitor.person_to_meet == approver.username,
                Visitor.status == VisitorStatus.WAITING
            ).order_by(Visitor.check_in_time.desc()).first()
            
            # If not found, try by name
            if not visitor:
                visitor = db.query(Visitor).filter(
                    Visitor.person_to_meet == approver.name,
                    Visitor.status == VisitorStatus.WAITING
                ).order_by(Visitor.check_in_time.desc()).first()
            
            # If still not found, try without status filter (maybe status changed)
            if not visitor:
                logger.warning(f"WEBHOOK: No WAITING visitors found, trying without status filter")
                visitor = db.query(Visitor).filter(
                    (Visitor.person_to_meet == approver.username) | (Visitor.person_to_meet == approver.name)
                ).order_by(Visitor.check_in_time.desc()).first()
                if visitor:
                    logger.warning(f"WEBHOOK: Found visitor {visitor.id} but status is {visitor.status}, not WAITING")
                    print(f"[WEBHOOK] WARNING: Visitor {visitor.id} status is {visitor.status}, expected WAITING")
            
            if visitor:
                logger.info(f"WEBHOOK: Found most recent visitor {visitor.id} with status: {visitor.status}, person_to_meet: {visitor.person_to_meet}")
                logger.info(f"WEBHOOK: Visitor name: {visitor.visitor_name}, check-in time: {visitor.check_in_time}")
                print(f"[WEBHOOK] Found visitor {visitor.id}, status: {visitor.status}, person_to_meet: {visitor.person_to_meet}")
                print(f"[WEBHOOK] Visitor: {visitor.visitor_name}, Time: {visitor.check_in_time}")
                
                # If multiple WAITING visitors, warn the approver
                if waiting_count > 1:
                    logger.warning(f"WEBHOOK: WARNING - {waiting_count} WAITING visitors found. Processing most recent: {visitor.id}")
                    print(f"[WEBHOOK] WARNING: {waiting_count} WAITING visitors. Processing most recent: {visitor.id}")
            else:
                logger.warning(f"WEBHOOK: No visitors found for {approver.username} (name: {approver.name})")
                print(f"[WEBHOOK] No visitors found for {approver.username}")
        
        if not visitor:
            if visitor_id:
                return _twiml_response(
                    f"Visitor {visitor_id} not found or you don't have permission to approve it."
                )
            else:
                # Check if there are any visitors at all (not just WAITING)
                all_visitors_count = db.query(Visitor).filter(
                    (Visitor.person_to_meet == approver.username) | (Visitor.person_to_meet == approver.name)
                ).count()
                
                if all_visitors_count > 0:
                    # There are visitors but none are WAITING
                    return _twiml_response(
                        f"You have {all_visitors_count} visitor(s), but none are pending approval.\n"
                        f"Please include visitor ID in your reply: APPROVED [VISITOR_ID]"
                    )
                else:
                    return _twiml_response(
                        "No pending visitor requests found. Please include visitor ID in your reply."
                    )
        
        # Handle REJECT_INITIATED - ask for reason
        if action == 'REJECT_INITIATED':
            # Store pending rejection
            pending_rejections[approver_phone] = str(visitor.id)
            logger.info(f"WEBHOOK: Rejection initiated for visitor {visitor.id}, asking for reason")
            print(f"[WEBHOOK] Rejection initiated for visitor {visitor.id}, waiting for reason")
            
            return _twiml_response(
                f"Visitor {visitor.id} rejection initiated.\n"
                f"Please provide the reason for rejection.\n"
                f"Reply with the reason (e.g., 'Not available today', 'Meeting cancelled', etc.)"
            )
        
        # Update visitor status
        old_status = visitor.status.value if visitor.status else "UNKNOWN"
        logger.info(f"WEBHOOK: Updating visitor {visitor.id} from {old_status} to {action}")
        logger.info(f"WEBHOOK: Visitor details - Name: {visitor.visitor_name}, person_to_meet: {visitor.person_to_meet}")
        print(f"[WEBHOOK] Updating visitor {visitor.id}: {old_status} -> {action}")
        print(f"[WEBHOOK] Visitor person_to_meet: {visitor.person_to_meet}, Approver username: {approver.username}, name: {approver.name}")
        
        if action == 'APPROVED':
            visitor.status = VisitorStatus.APPROVED
            visitor.rejection_reason = None  # Clear any rejection reason if approving
            status_message = "approved"
        else:
            # This shouldn't happen here, but handle it just in case
            visitor.status = VisitorStatus.REJECTED
            status_message = "rejected"
        
        try:
            db.commit()
            db.refresh(visitor)
            new_status = visitor.status.value if visitor.status else "UNKNOWN"
            logger.info("=" * 60)
            logger.info(f"WEBHOOK: SUCCESS - Visitor {visitor.id} status updated from {old_status} to {new_status}")
            logger.info(f"WEBHOOK: Updated by {approver.username} (phone: {approver_phone}) via SMS")
            logger.info("=" * 60)
            print(f"[WEBHOOK] SUCCESS: Visitor {visitor.id} status: {old_status} -> {new_status}")
            print(f"[WEBHOOK] Database commit successful")
            
            # Verify the update by querying fresh from database
            db.expire_all()  # Expire all cached objects
            verify_visitor = db.query(Visitor).filter(Visitor.id == visitor.id).first()
            if verify_visitor:
                logger.info(f"WEBHOOK: Verification - Visitor {verify_visitor.id} status in DB: {verify_visitor.status.value}")
                logger.info(f"WEBHOOK: Verification - person_to_meet: {verify_visitor.person_to_meet}")
                print(f"[WEBHOOK] Verification: Visitor {verify_visitor.id} status in DB: {verify_visitor.status.value}")
            else:
                logger.error(f"WEBHOOK: Verification FAILED - Visitor {visitor.id} not found after update!")
                print(f"[WEBHOOK] ERROR: Visitor not found after update!")
        except Exception as e:
            db.rollback()
            logger.error("=" * 60)
            logger.error(f"WEBHOOK: ERROR - Failed to update visitor status: {e}")
            logger.error("=" * 60, exc_info=True)
            print(f"[WEBHOOK] ERROR: Failed to update status: {e}")
            import traceback
            print(traceback.format_exc())
            return _twiml_response("Error updating visitor status. Please try again or use the dashboard.")
        
        # Send confirmation SMS
        confirmation_message = (
            f"Visitor {visitor.id} has been {status_message}.\n"
            f"Name: {visitor.visitor_name}\n"
            f"Status: {action}"
        )
        
        return _twiml_response(confirmation_message)
        
    except Exception as e:
        logger.error(f"Error processing SMS webhook: {e}", exc_info=True)
        return _twiml_response("An error occurred. Please try again or use the dashboard.")


def _twiml_response(message: str) -> str:
    """
    Generate TwiML XML response for SMS.
    
    Args:
        message: Message to send back to the sender
        
    Returns:
        TwiML XML string
    """
    # Escape XML special characters
    message = message.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{message}</Message>
</Response>"""

