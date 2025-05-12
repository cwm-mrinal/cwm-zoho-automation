import json
import os
import boto3
import logging

# Logger Setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Bedrock Agent ARNs
AGENT_ARNS = {
    "cost_optimization": "arn:aws:bedrock:ap-south-1:207567766326:agent/KJV8XSDDPE",
    "security": "arn:aws:bedrock:ap-south-1:207567766326:agent/R4JDDS6KMH",
    "alarm": "arn:aws:bedrock:ap-south-1:207567766326:agent/TNMOFURJPN",
    "custom": "arn:aws:bedrock:ap-south-1:207567766326:agent/KHJEQVEPTX",
    "main": "arn:aws:bedrock:ap-south-1:207567766326:agent/JTZYLRXN94"
}

# Alias IDs
AGENT_ALIASES = {
    "main": "NBFWB6OE7D",
    "cost_optimization": "5VIWBP9MJV",
    "security": "CVA7RE8WRO",
    "alarm": "J4EHTV8JJ7",
    "custom": "FEMLOFVEYX"
}

# AWS clients
bedrock = boto3.client("bedrock-agent-runtime")
sns = boto3.client("sns")
sqs = boto3.client("sqs")
comprehend = boto3.client("comprehend")
translate = boto3.client("translate")

# Environment variables
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN")
DLQ_URL = os.environ.get("DLQ_URL")


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    try:
        body = json.loads(event["body"]) if isinstance(event.get("body"), str) else event

        ticket_id = body.get("ticketId", "test-session")
        ticket_subject = body.get("ticketSubject", "")
        ticket_body = body.get("ticketBody", "")
        customer_email = body.get("customerEmail")

        if not ticket_subject or not ticket_body:
            return {
                "statusCode": 400,
                "body": "Missing 'ticketSubject' or 'ticketBody' in input"
            }

        ticket_description = f"{ticket_subject}\n\n{ticket_body}"
        logger.info("Original ticket description: %s", ticket_description)

        # Detect Language
        lang_response = comprehend.detect_dominant_language(Text=ticket_description)
        language_code = lang_response["Languages"][0]["LanguageCode"]
        logger.info("Detected language: %s", language_code)

        # Translate to English if needed
        if language_code != "en":
            logger.info("Translating ticket from %s to en.", language_code)
            translation = translate.translate_text(
                Text=ticket_description,
                SourceLanguageCode=language_code,
                TargetLanguageCode="en"
            )
            translated_description = translation["TranslatedText"]
            logger.info("Translation complete.")
        else:
            translated_description = ticket_description

        # Step 1: Classify using Main Agent
        classification_prompt = f"""
You are a support ticket classifier. Your task is to analyze the customer's issue and return a JSON response with two fields:
- category: one of ['cost_optimization', 'security', 'alarm', 'custom']
- confidence: a float between 0 and 1 representing your confidence level.

Example Output:
{{"category": "cost_optimization", "confidence": 0.9}}

Customer Ticket:
\"{translated_description}\""""

        main_response = invoke_bedrock_agent(
            AGENT_ARNS["main"],
            ticket_id,
            classification_prompt,
            alias_id=AGENT_ALIASES["main"]
        )

        logger.info("Main agent response: %s", main_response)

        classification = main_response.get("category", "").lower()
        confidence = float(main_response.get("confidence", 0.0))

        if confidence < 0.7 or not classification:
            logger.warning("Low confidence classification. Returning fallback.")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status": "fallback",
                    "message": "Low confidence score. Manual review needed.",
                    "classification": classification,
                    "confidence": confidence
                })
            }

        # Step 2: Route to appropriate Agent
        agent_key = classification
        agent_arn = AGENT_ARNS.get(agent_key)
        alias_id = AGENT_ALIASES.get(agent_key)

        logger.info("Invoking agent: %s", agent_key)

        response_data = invoke_bedrock_agent(
            agent_arn,
            ticket_id,
            translated_description,
            alias_id=alias_id
        )

        logger.info("Agent response: %s", response_data)

        reply_text = response_data.get("reply") or \
                     response_data.get("message") or \
                     response_data.get("raw_response", "Thank you for reaching out. We will assist you shortly.")

        reply_text = reply_text.replace("\\n", "\n")

        # For "custom" category, send email
        if classification == "custom":
            logger.info("Sending email via SNS...")
            send_email_via_sns(customer_email, ticket_subject, ticket_body, reply_text)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status": "success",
                "ticketId": ticket_id,
                "customerEmail": customer_email,
                "category": classification,
                "confidence": confidence,
                "language": language_code,
                "agent_used": agent_key,
                "reply": reply_text
            })
        }

    except Exception as e:
        error_message = str(e)
        logger.error("Unhandled Exception: %s", error_message)

        if DLQ_URL:
            try:
                sqs.send_message(
                    QueueUrl=DLQ_URL,
                    MessageBody=json.dumps({
                        "error": error_message,
                        "originalEvent": event
                    })
                )
                logger.info("Event pushed to DLQ.")
            except Exception as dlq_error:
                logger.error("Failed to push to DLQ: %s", str(dlq_error))

        return {
            "statusCode": 500,
            "body": json.dumps({"error": error_message})
        }


def invoke_bedrock_agent(agent_arn, session_id, input_text, alias_id=None):
    agent_id = agent_arn.split("/")[-1]
    logger.info("Invoking Bedrock Agent: %s", agent_id)

    response_stream = bedrock.invoke_agent(
        agentId=agent_id,
        agentAliasId=alias_id,
        sessionId=session_id,
        inputText=input_text
    )

    final_output = ""
    for event in response_stream["completion"]:
        chunk = event.get("chunk")
        if chunk and "bytes" in chunk:
            final_output += chunk["bytes"].decode("utf-8")

    logger.info("Bedrock agent raw output: %s", final_output)

    try:
        return json.loads(final_output)
    except json.JSONDecodeError:
        logger.warning("Agent output not JSON. Returning raw response.")
        return {"raw_response": final_output}


def send_email_via_sns(customer_email, subject, original_body, reply_text):
    if not SNS_TOPIC_ARN:
        raise ValueError("SNS_TOPIC_ARN is not set in environment variables.")

    message = f"""
Dear Customer,

Thank you for reaching out to our Workmates Support Team.

{reply_text}

If you have any further questions or need additional assistance, feel free to reply to this email.

Best regards,  
Workmates Support Team
    """

    response = sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=f"Re: {subject}",
        Message=message,
        MessageAttributes={
            "customerEmail": {
                "DataType": "String",
                "StringValue": customer_email
            }
        }
    )

    logger.info("Email sent via SNS. Message ID: %s", response['MessageId'])
