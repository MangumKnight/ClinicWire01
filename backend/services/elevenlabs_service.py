"""
ElevenLabs Service
Handles voice calls using ElevenLabs Conversational AI
"""

import os
import json
import logging
from typing import Dict, Any, Optional

import requests

logger = logging.getLogger(__name__)

class ElevenLabsService:
    def __init__(self):
        self.api_key = os.getenv("ELEVENLABS_API_KEY")
        self.agent_id = "agent_7701k252vk5cek9azpbvm6b0n8xz"  # From the HTML
        self.phone_number_id = "phnum_7501k11pppxgfd7r0a228ncpvkqm"  # From the HTML
        self.api_url = "https://api.elevenlabs.io/v1/convai/twilio/outbound-call"
    
    def is_configured(self) -> bool:
        """Check if ElevenLabs is properly configured"""
        return bool(self.api_key)
    
    def make_call(
        self,
        to_number: str,
        patient_name: str,
        doctor_name: str,
        date_sent: str,
        fax_number: str,
        webhook_url: str
    ) -> Optional[Dict[str, Any]]:
        """
        Make outbound call using ElevenLabs Conversational AI
        """
        if not self.is_configured():
            logger.error("ElevenLabs not configured")
            return None
        
        try:
            # Format phone number
            if not to_number.startswith("+"):
                # Clean and format number
                cleaned = "".join(filter(str.isdigit, to_number))
                if not cleaned.startswith("1") and len(cleaned) == 10:
                    to_number = f"+1{cleaned}"
                else:
                    to_number = f"+{cleaned}"
            
            # Prepare payload (matching the HTML's structure)
            payload = {
                "agent_id": self.agent_id,
                "agent_phone_number_id": self.phone_number_id,
                "to_number": to_number,
                "conversation_initiation_client_data": {
                    "dynamic_variables": {
                        "patient_name": patient_name,
                        "doctor_name": doctor_name,
                        "date_sent": date_sent,
                        "fax_number": fax_number
                    }
                },
                "webhook_url": webhook_url
            }
            
            # Make API call
            headers = {
                "xi-api-key": self.api_key,
                "Content-Type": "application/json"
            }
            
            response = requests.post(
                self.api_url,
                json=payload,
                headers=headers,
                timeout=30
            )
            
            if response.ok:
                result = response.json()
                logger.info(f"ElevenLabs call initiated: {result}")
                
                # Extract both conversation ID and Twilio call SID
                conversation_id = result.get("conversation_id") or result.get("call_id") or result.get("id")
                twilio_sid = result.get("callSid")  # This is what Twilio uses for webhooks
                
                return {
                    "call_id": conversation_id,  # ElevenLabs conversation ID
                    "twilio_sid": twilio_sid,    # Twilio call SID for webhooks
                    "status": "initiated",
                    "response": result
                }
            else:
                logger.error(f"ElevenLabs API error: {response.status_code} - {response.text}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error calling ElevenLabs: {e}")
            return None
        except Exception as e:
            logger.error(f"Error making ElevenLabs call: {e}")
            return None