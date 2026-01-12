"""
Twilio Service
Handles SMS sending and call management
"""

import os
import logging
from typing import Dict, Any, Optional

from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

logger = logging.getLogger(__name__)

class TwilioService:
    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.from_number = os.getenv("TWILIO_FROM_NUMBER")
        self.client = None
        self._initialize_client()
    
    def _initialize_client(self):
        """Initialize Twilio client"""
        if self.account_sid and self.auth_token:
            try:
                self.client = Client(self.account_sid, self.auth_token)
                logger.info("Twilio client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize Twilio client: {e}")
                self.client = None
        else:
            logger.warning("Twilio credentials not configured")
    
    def is_configured(self) -> bool:
        """Check if Twilio is properly configured"""
        return bool(self.client and self.from_number)
    
    def send_sms(self, to_number: str, message: str) -> Optional[str]:
        """
        Send SMS message
        Returns message SID if successful, None otherwise
        """
        if not self.is_configured():
            logger.error("Twilio not configured")
            return None
        
        try:
            # Format phone number
            if not to_number.startswith("+"):
                # Assume US number if no country code
                if not to_number.startswith("1"):
                    to_number = f"+1{to_number}"
                else:
                    to_number = f"+{to_number}"
            
            # Remove any non-digit characters except +
            to_number = "+" + "".join(filter(str.isdigit, to_number))
            
            # Send message
            message_obj = self.client.messages.create(
                body=message,
                from_=self.from_number,
                to=to_number
            )
            
            logger.info(f"SMS sent successfully to {to_number}: {message_obj.sid}")
            return message_obj.sid
            
        except TwilioRestException as e:
            logger.error(f"Twilio error sending SMS: {e}")
            return None
        except Exception as e:
            logger.error(f"Error sending SMS: {e}")
            return None
    
    def make_call(self, to_number: str, twiml_url: str, status_callback: str = None) -> Optional[Dict[str, Any]]:
        """
        Make outbound call (not used in current flow since ElevenLabs handles calls)
        Kept for future use if switching to Twilio for calls
        """
        if not self.is_configured():
            logger.error("Twilio not configured")
            return None
        
        try:
            # Format phone number
            if not to_number.startswith("+"):
                if not to_number.startswith("1"):
                    to_number = f"+1{to_number}"
                else:
                    to_number = f"+{to_number}"
            
            # Make call
            call = self.client.calls.create(
                to=to_number,
                from_=self.from_number,
                url=twiml_url,
                status_callback=status_callback,
                status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
                status_callback_method='POST'
            )
            
            logger.info(f"Call initiated to {to_number}: {call.sid}")
            return {
                "call_sid": call.sid,
                "status": call.status,
                "to": call.to,
                "from": call.from_
            }
            
        except TwilioRestException as e:
            logger.error(f"Twilio error making call: {e}")
            return None
        except Exception as e:
            logger.error(f"Error making call: {e}")
            return None