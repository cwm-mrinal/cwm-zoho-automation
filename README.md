### Automating Support Ticket Classification with AWS Lambda, Bedrock Agents, and SNS

In today's fast-paced business environment, automating support ticket management can streamline processes and improve customer experience. AWS provides a powerful suite of tools that can help businesses achieve this, and in this blog, we’ll walk through an AWS Lambda function that integrates several AWS services to automatically classify support tickets, detect language, translate text, and notify internal teams via email.

This solution leverages **AWS Lambda**, **Amazon Bedrock**, **Amazon Comprehend**, **Amazon Translate**, and **Amazon SNS** to provide a seamless support ticket management flow. Let’s break it down!

#### Use Case: Automating Support Ticket Classification

When a customer submits a support ticket through an API Gateway, it triggers a Lambda function. The Lambda function is responsible for:

1. **Classifying the ticket** based on its content into categories like **Cost Optimization**, **Security**, **Alarms**, and **Custom**.
2. **Detecting the language** of the ticket using Amazon **Comprehend**.
3. **Translating the content** into English (if needed) using Amazon **Translate**.
4. **Routing the ticket** to the appropriate agent using **Amazon Bedrock**.
5. **Sending a response** back to the customer, and notifying the internal support team via **SNS** for custom tickets.

Let’s dive deeper into the Lambda function that orchestrates this entire process.

---

### Lambda Function Overview

Here’s the complete Lambda function:

```python
import json
import os
import boto3
import logging

# Logger Setup
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Bedrock Agent ARNs
AGENT_ARNS = {
    "cost_optimization": "arn:aws:bedrock:<Region>:<Account-Id>:agent/ABCXXXXX",
    "security": "arn:aws:bedrock:<Region>:<Account-Id>:agent/ABCXXXXX",
    "alarm": "arn:aws:bedrock:<Region>:<Account-Id>:agent/ABCXXXXX",
    "custom": "arn:aws:bedrock:<Region>:<Account-Id>:agent/ABCXXXXX",
    "main": "arn:aws:bedrock:<Region>:<Account-Id>:agent/ABCXXXXX"
}

# Alias IDs
AGENT_ALIASES = {
    "main": "ABCXXXXXXXXXXX",
    "cost_optimization": "ABCXXXXXXXXXXX",
    "security": "ABCXXXXXXXXXXX",
    "alarm": "ABCXXXXXXXXXXX",
    "custom": "ABCXXXXXXXXXXX"
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
```


---

### Key AWS Services Utilized

1. **Amazon Bedrock**: Used to classify tickets based on predefined agents. Each agent specializes in a category, such as Cost Optimization, Security, or Alarms.

2. **Amazon Comprehend**: Detects the dominant language of the ticket to ensure proper translation if needed.

3. **Amazon Translate**: Translates the ticket content to English if the ticket was submitted in another language.

4. **Amazon SNS**: Sends an email to the internal support team for **custom** category tickets. It’s a vital service to notify support agents about issues that need their attention.

5. **Amazon SQS**: Used for dead-letter queue (DLQ) functionality. If an error occurs, the event gets pushed to the DLQ for future processing or investigation.

---

### Workflow Summary

1. **Ticket Submission**: A ticket is submitted via an API Gateway and triggers the Lambda function.
2. **Language Detection**: The Lambda function uses Amazon Comprehend to detect the language of the ticket.
3. **Translation (if necessary)**: If the ticket is not in English, it is translated using Amazon Translate.
4. **Classification**: The ticket is classified using the appropriate Bedrock agent. If the classification confidence is below 70%, a fallback response is returned.
5. **Ticket Routing**: The ticket is routed to the relevant agent based on the classification. If it’s a custom ticket, an email is sent to internal support via SNS.
6. **Response to Customer**: A reply is sent back to the customer via SNS.

---

### Conclusion

This AWS Lambda-based automation simplifies the process of ticket classification and response generation. By integrating services like **Amazon Bedrock**, **Comprehend**, **Translate**, and **SNS**, you can efficiently handle customer support tickets, reducing manual effort and improving response times. This solution ensures that tickets are automatically categorized, translated, and routed to the appropriate agents, while also notifying the internal support team for immediate action on critical tickets.

If you’re interested in similar solutions or need more information about AWS Lambda, feel free to leave a comment below!
