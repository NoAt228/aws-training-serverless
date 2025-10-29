import os
import json
import boto3
from urllib.parse import unquote_plus

# Инициализируем клиенты AWS один раз при "холодном старте" функции.
# Это эффективнее, чем создавать их при каждом вызове.

# Получаем имя таблицы из переменных окружения, которые задаются в SAM-шаблоне.
TABLE_NAME = os.environ.get('TABLE_NAME')
s3_client = boto3.client('s3')
dynamodb_resource = boto3.resource('dynamodb')
table = dynamodb_resource.Table(TABLE_NAME)

def lambda_handler(event, context):
    """Главный обработчик. Определяет источник события и маршрутизирует вызов."""
    print("Event received:", json.dumps(event))

    # Событие от S3 всегда содержит ключ 'Records'.
    if 'Records' in event:
        return handle_s3_event(event)
    # Событие от API Gateway (через SAM) содержит 'httpMethod'.
    elif 'httpMethod' in event:
        return handle_api_gateway_event(event)
    
    # Если источник неизвестен, возвращаем ошибку.
    return {'statusCode': 400, 'body': json.dumps('Unknown event source')}

def handle_s3_event(event):
    """Обрабатывает событие загрузки файла в S3."""
    try:
        # Извлекаем имя бакета и ключ объекта (имя файла) из структуры события.
        bucket_name = event['Records'][0]['s3']['bucket']['name']
        # Имя файла в URL может быть закодировано (например, пробелы как %20), декодируем его.
        object_key = unquote_plus(event['Records'][0]['s3']['object']['key'])

        # Делаем запрос head_object, чтобы получить метаданные файла (размер, тип), не скачивая его целиком.
        response = s3_client.head_object(Bucket=bucket_name, Key=object_key)
        
        # Готовим элемент для записи в DynamoDB.
        item = {
            'imageName': object_key,
            'size': response['ContentLength'],
            'contentType': response['ContentType'],
            'lastModified': response['LastModified'].isoformat()
        }

        # Записываем метаданные в таблицу.
        table.put_item(Item=item)
        print(f"Successfully processed {object_key}.")
        
        return {'statusCode': 200}

    except Exception as e:
        print(f"Error processing S3 event: {e}")
        # Перевыбрасываем исключение, чтобы вызов Lambda завершился с ошибкой.
        # Это важно для срабатывания алармов и Dead Letter Queues.
        raise e

def handle_api_gateway_event(event):
    """Обрабатывает GET-запрос от API Gateway."""
    try:
        # Извлекаем имя файла из path-параметров запроса (например, /images/my-photo.jpg).
        image_name = event['pathParameters']['imageName']
        
        # Получаем элемент из DynamoDB по первичному ключу.
        response = table.get_item(Key={'imageName': image_name})

        if 'Item' in response:
            # DynamoDB возвращает числа в своем формате Decimal. Конвертируем их в int для корректного JSON.
            item = response['Item']
            item['size'] = int(item['size'])
            return {
                'statusCode': 200,
                'body': json.dumps(item),
                'headers': {'Content-Type': 'application/json'}
            }
        else:
            # Если элемент не найден, возвращаем 404.
            return {'statusCode': 404, 'body': json.dumps({'error': 'Image not found'})}
            
    except Exception as e:
        print(f"Error processing API Gateway event: {e}")
        return {'statusCode': 500, 'body': json.dumps({'error': 'Internal server error'})}


