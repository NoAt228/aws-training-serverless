# Serverless на AWS: Полное Production-Ready Руководство

Это руководство — твой билет в мир взрослого serverless. Мы не будем играть в песочнице. Мы построим систему, которую не стыдно выкатить в прод: безопасную, отказоустойчивую, наблюдаемую и полностью автоматизированную.

---

## Краткий ликбез (Теория)

### 1. Зачем лямбде своя VPC?

По умолчанию лямбда живёт в общей сети AWS и видит интернет. Это удобно для "hello world", но в проде — дыра в безопасности.
- **Изоляция:** Помещая лямбду в **свою VPC**, мы отрезаем её от внешнего мира. Доступ — только через API Gateway.
- **Доступ к приватным ресурсам:** Только из VPC лямбда может безопасно общаться с твоей базой данных (RDS) или кэшем (ElastiCache), не выставляя их наружу.

**Проблема:** Лямбда в *приватной* подсети слепа и не видит интернет.
- **Решение 1 (Доступ наружу):** Ставим **NAT Gateway** в *публичной* подсети. Весь исходящий трафик от лямбды идёт через него. Это стоит денег.
- **Решение 2 (Доступ к сервисам AWS):** Используем **VPC Endpoints**. Это "приватные двери" к сервисам вроде S3, DynamoDB, SQS прямо из твоей VPC. Трафик не покидает сеть AWS, это быстрее, безопаснее и часто дешевле (Gateway Endpoints для S3/DynamoDB вообще бесплатны).

### 2. Что такое Dead Letter Queue (DLQ)?

Если лямбда, вызванная асинхронно (например, триггером от S3), падает, AWS попробует запустить её ещё пару раз и... выбросит событие. Оно потеряно навсегда. **DLQ** — это "морг" для таких событий (обычно SQS-очередь), куда лямбда автоматически скинет сбойное сообщение. Это даёт тебе шанс разобраться в причинах и обработать его позже.

### 3. Зачем нам AWS SAM?

**AWS Serverless Application Model (SAM)** — это надстройка над CloudFormation, заточенная под serverless. Пишешь меньше YAML-кода, получаешь тот же результат. Ресурсы вроде `AWS::Serverless::Function` или `AWS::Serverless::Api` сильно упрощают жизнь.

---

## Практика: Строим Production-Ready Систему

### Архитектура

Мы разделим нашу систему на 5 независимых стеков:
1.  `network-stack.yml`: Сетевой фундамент (VPC, подсети, NAT, эндпоинты).
2.  `storage-stack.yml`: Хранилища (S3, DynamoDB).
3.  `application-stack.yml`: Логика (Lambda, API Gateway, DLQ).
4.  `monitoring-stack.yml`: Глаза и уши (CloudWatch Alarms).
5.  `ci-cd-stack.yml`: Автоматизация (CodePipeline, CodeBuild).

---

### Часть 0: Код и структура приложения

Прежде чем строить инфраструктуру, посмотрим, что мы будем деплоить. Наше приложение — это простая Python-лямбда, которая умеет делать две вещи: реагировать на загрузку файла в S3 и отвечать на GET-запросы через API Gateway.

#### Структура проекта

Твой проект должен выглядеть так. Все `.yml` файлы лежат в корне, а код лямбды — в папке `src`.

```
/home/bushido/aws/serverless/
├── .github/
│   └── workflows/
│       └── deploy.yml
├── src/
│   ├── app.py              # Код лямбда-функции
│   └── requirements.txt    # Зависимости Python
├── application-stack.yml
├── iam-github-role.yml
├── monitoring-stack.yml
├── network-stack.yml
└── storage-stack.yml
```

#### Код приложения (`src/app.py`)

Это сердце нашего сервиса. Один обработчик `lambda_handler` рулит двумя типами событий.

```python
import os
import json
import boto3
import logging

# Настраиваем логирование, чтобы видеть, что происходит внутри
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Получаем имя таблицы из переменных окружения, заданных в SAM-шаблоне
TABLE_NAME = os.environ.get('TABLE_NAME')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(TABLE_NAME)

def lambda_handler(event, context):
    logger.info(f"Received event: {json.dumps(event)}")

    # Маршрутизация: определяем, откуда пришло событие
    # 1. Событие от API Gateway (синхронный вызов)
    if 'httpMethod' in event:
        return handle_api_gateway(event)
    # 2. Событие от S3 (асинхронный вызов)
    elif 'Records' in event and event['Records'][0]['eventSource'] == 'aws:s3':
        return handle_s3_event(event)
    # 3. Неизвестный тип события
    else:
        logger.warning("Unknown event type")
        return {'statusCode': 400, 'body': json.dumps('Unknown event type')}

def handle_api_gateway(event):
    http_method = event['httpMethod']
    path = event['path']

    if http_method == 'GET' and '/images/' in path:
        try:
            # Извлекаем имя файла из пути /images/{imageName}
            image_name = path.split('/images/')[-1]
            response = table.get_item(Key={'imageName': image_name})

            if 'Item' in response:
                return {
                    'statusCode': 200,
                    'headers': {'Content-Type': 'application/json'},
                    'body': json.dumps(response['Item'])
                }
            else:
                return {'statusCode': 404, 'body': json.dumps({'message': 'Image not found'})}

        except Exception as e:
            logger.error(f"Error getting item from DynamoDB: {e}")
            return {'statusCode': 500, 'body': json.dumps({'message': 'Internal server error'})}
    else:
        return {'statusCode': 400, 'body': json.dumps({'message': 'Unsupported route'})}

def handle_s3_event(event):
    try:
        for record in event['Records']:
            bucket_name = record['s3']['bucket']['name']
            object_key = record['s3']['object']['key']

            logger.info(f"New object \"{object_key}\" uploaded to bucket \"{bucket_name}\".")

            # Здесь могла бы быть логика обработки файла: 
            # создание превью, анализ метаданных, запись в DynamoDB и т.д.
            # Для примера просто логируем.

            # Пример записи в DynamoDB:
            # table.put_item(
            #     Item={
            #         'imageName': object_key,
            #         'bucket': bucket_name,
            #         'size': record['s3']['object']['size']
            #     }
            # )

    except Exception as e:
        logger.error(f"Error processing S3 event: {e}")
        # Если здесь произойдет ошибка, событие улетит в DLQ
        raise e

    return {'statusCode': 200, 'body': json.dumps('S3 event processed successfully')}

```

#### Зависимости (`src/requirements.txt`)

Для этого примера внешние зависимости не нужны, так как `boto3` уже входит в состав Python-окружения AWS Lambda. Если бы они были (например, `requests` или `pillow`), их нужно было бы указать здесь. `sam build` автоматически их установит.

```text
# boto3 is included in the AWS Lambda runtime environment.
# Add other dependencies here, for example:
# requests==2.28.1
# pillow==9.4.0
```

---

### Часть 1: Фундамент (Сеть и Хранилища)

#### 1.1. `network-stack.yml`

**Назначение:** Создаёт пуленепробиваемую сеть. Разворачивается один раз и живёт вечно.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'Network Stack: VPC, Public/Private Subnets, NAT GW, VPC Endpoints. Production-ready.'

Resources:
  # --- VPC ---
  VPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
      EnableDnsSupport: true
      EnableDnsHostnames: true
      Tags:
        - Key: Name
          Value: !Sub '${AWS::StackName}-VPC'

  # --- Subnets (в двух зонах доступности для отказоустойчивости) ---
  PublicSubnet1:
    Type: AWS::EC2::Subnet
    Properties: { VpcId: !Ref VPC, CidrBlock: 10.0.1.0/24, AvailabilityZone: !Select [ 0, !GetAZs '' ], MapPublicIpOnLaunch: true, Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PublicSubnet1'}] }
  PublicSubnet2:
    Type: AWS::EC2::Subnet
    Properties: { VpcId: !Ref VPC, CidrBlock: 10.0.2.0/24, AvailabilityZone: !Select [ 1, !GetAZs '' ], MapPublicIpOnLaunch: true, Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PublicSubnet2'}] }
  PrivateSubnet1:
    Type: AWS::EC2::Subnet
    Properties: { VpcId: !Ref VPC, CidrBlock: 10.0.101.0/24, AvailabilityZone: !Select [ 0, !GetAZs '' ], Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PrivateSubnet1'}] }
  PrivateSubnet2:
    Type: AWS::EC2::Subnet
    Properties: { VpcId: !Ref VPC, CidrBlock: 10.0.102.0/24, AvailabilityZone: !Select [ 1, !GetAZs '' ], Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PrivateSubnet2'}] }

  # --- Internet Gateway (для доступа в интернет из публичных подсетей) ---
  InternetGateway:
    Type: AWS::EC2::InternetGateway
    Properties: { Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-IGW'}] }
  VPCGatewayAttachment:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties: { VpcId: !Ref VPC, InternetGatewayId: !Ref InternetGateway }

  # --- NAT Gateway (для доступа в интернет из приватных подсетей) ---
  NatGatewayEIP:
    Type: AWS::EC2::EIP
    Properties: { Domain: vpc }
  NatGateway:
    Type: AWS::EC2::NatGateway
    Properties: { AllocationId: !GetAtt NatGatewayEIP.AllocationId, SubnetId: !Ref PublicSubnet1 }

  # --- Route Tables ---
  PublicRouteTable:
    Type: AWS::EC2::RouteTable
    Properties: { VpcId: !Ref VPC, Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PublicRouteTable'}] }
  PublicRoute:
    Type: AWS::EC2::Route
    DependsOn: VPCGatewayAttachment
    Properties: { RouteTableId: !Ref PublicRouteTable, DestinationCidrBlock: 0.0.0.0/0, GatewayId: !Ref InternetGateway }
  PublicSubnet1RouteTableAssociation: { Type: AWS::EC2::SubnetRouteTableAssociation, Properties: { SubnetId: !Ref PublicSubnet1, RouteTableId: !Ref PublicRouteTable } }
  PublicSubnet2RouteTableAssociation: { Type: AWS::EC2::SubnetRouteTableAssociation, Properties: { SubnetId: !Ref PublicSubnet2, RouteTableId: !Ref PublicRouteTable } }

  PrivateRouteTable:
    Type: AWS::EC2::RouteTable
    Properties: { VpcId: !Ref VPC, Tags: [{Key: Name, Value: !Sub '${AWS::StackName}-PrivateRouteTable'}] }
  PrivateRoute:
    Type: AWS::EC2::Route
    Properties: { RouteTableId: !Ref PrivateRouteTable, DestinationCidrBlock: 0.0.0.0/0, NatGatewayId: !Ref NatGateway }
  PrivateSubnet1RouteTableAssociation: { Type: AWS::EC2::SubnetRouteTableAssociation, Properties: { SubnetId: !Ref PrivateSubnet1, RouteTableId: !Ref PrivateRouteTable } }
  PrivateSubnet2RouteTableAssociation: { Type: AWS::EC2::SubnetRouteTableAssociation, Properties: { SubnetId: !Ref PrivateSubnet2, RouteTableId: !Ref PrivateRouteTable } }

  # --- VPC Endpoints (ключ к успеху!) ---
  S3GatewayEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcId: !Ref VPC
      ServiceName: !Sub 'com.amazonaws.${AWS::Region}.s3'
      RouteTableIds: [!Ref PrivateRouteTable]
  DynamoDBGatewayEndpoint:
    Type: AWS::EC2::VPCEndpoint
    Properties:
      VpcId: !Ref VPC
      ServiceName: !Sub 'com.amazonaws.${AWS::Region}.dynamodb'
      RouteTableIds: [!Ref PrivateRouteTable]

Outputs:
  VpcId:
    Description: "ID of the VPC"
    Value: !Ref VPC
    Export: { Name: !Sub "${AWS::StackName}-VpcId" }
  PrivateSubnetIds:
    Description: "Comma-delimited list of private subnet IDs"
    Value: !Join [ ",", [ !Ref PrivateSubnet1, !Ref PrivateSubnet2 ] ]
    Export: { Name: !Sub "${AWS::StackName}-PrivateSubnetIds" }
```

#### 1.2. `storage-stack.yml`

**Назначение:** Создаёт S3 бакет и DynamoDB таблицу с настройками для прода.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: 'Storage Stack: S3 bucket for uploads and DynamoDB table for metadata.'

Resources:
  ImageBucket:
    Type: AWS::S3::Bucket
    Properties:
      # Блокируем весь публичный доступ. Безопасность — по умолчанию.
      PublicAccessBlockConfiguration:
        BlockPublicAcls: true
        BlockPublicPolicy: true
        IgnorePublicAcls: true
        RestrictPublicBuckets: true
      # Включаем версионирование на случай, если понадобится откатить удалённый объект
      VersioningConfiguration:
        Status: Enabled

  MetadataTable:
    Type: AWS::DynamoDB::Table
    Properties:
      AttributeDefinitions:
        - AttributeName: "imageName"
          AttributeType: "S"
      KeySchema:
        - AttributeName: "imageName"
          KeyType: "HASH"
      BillingMode: PAY_PER_REQUEST # Идеально для serverless с непредсказуемой нагрузкой
      # Включаем восстановление на любой момент времени за последние 35 дней. Обязательно для прода.
      PointInTimeRecoverySpecification:
        PointInTimeRecoveryEnabled: true

Outputs:
  ImageBucketName:
    Description: "Name of the S3 bucket for image uploads"
    Value: !Ref ImageBucket
    Export: { Name: !Sub "${AWS::StackName}-ImageBucketName" }
  MetadataTableName:
    Description: "Name of the DynamoDB table for metadata"
    Value: !Ref MetadataTable
    Export: { Name: !Sub "${AWS::StackName}-MetadataTableName" }
```

---

### Часть 2: Приложение и Мониторинг

#### 2.1. `application-stack.yml`

**Назначение:** Сердце системы. Лямбда, API, DLQ и все нужные права.

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: 'Application Stack: VPC-enabled Lambda, API Gateway, DLQ, and IAM Roles.'

Parameters:
  NetworkStackName:
    Type: String
    Description: "Name of the network stack (e.g., my-app-network)."
  StorageStackName:
    Type: String
    Description: "Name of the storage stack (e.g., my-app-storage)."

Resources:
  # Явно определяем API Gateway, но без автоматического деплоя (убрали StageName)
  MyApi:
    Type: AWS::Serverless::Api
    Properties:
      TracingEnabled: true

  ImageEventsDLQ:
    Type: AWS::SQS::Queue

  MetadataFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: src/
      Handler: app.lambda_handler
      Runtime: python3.9
      Timeout: 15
      Tracing: Active
      DeadLetterQueue:
        Type: SQS
        TargetArn: !GetAtt ImageEventsDLQ.Arn
      Environment:
        Variables:
          TABLE_NAME: !ImportValue
            Fn::Sub: "${StorageStackName}-MetadataTableName"
      VpcConfig:
        SecurityGroupIds:
          - !Ref LambdaSecurityGroup
        SubnetIds: !Split [ ",", !ImportValue { Fn::Sub: "${NetworkStackName}-PrivateSubnetIds" } ]
      Policies:
        - S3ReadPolicy:
            BucketName: !ImportValue
              Fn::Sub: "${StorageStackName}-ImageBucketName"
        - DynamoDBCrudPolicy:
            TableName: !ImportValue
              Fn::Sub: "${StorageStackName}-MetadataTableName"
        - SQSSendMessagePolicy:
            QueueName: !GetAtt ImageEventsDLQ.QueueName
        - VPCAccessPolicy: {}
    Events:
      S3Upload:
        Type: S3
        Properties:
          Bucket: !ImportValue
            Fn::Sub: '${StorageStackName}-ImageBucketName'
          Events: s3:ObjectCreated:*
      GetMetadata:
        Type: Api
        Properties:
          RestApiId: !Ref MyApi
          Path: /images/{imageName}
          Method: get

  # ФИНАЛЬНЫЙ ФИКС: Явно создаём ресурс развертывания
  ApiDeployment:
    Type: AWS::ApiGateway::Deployment
    Properties:
      RestApiId: !Ref MyApi
    # И самое главное: указываем, что он должен ждать создания прав доступа для лямбды.
    # SAM автоматически создает ресурс прав с именем <FunctionLogicalId><EventId>ApiPermission
    DependsOn: MetadataFunctionGetMetadataApiPermission

  # ФИНАЛЬНЫЙ ФИКС: Явно создаём ресурс стадии (Stage)
  ApiProdStage:
    Type: AWS::ApiGateway::Stage
    Properties:
      StageName: Prod
      RestApiId: !Ref MyApi
      DeploymentId: !Ref ApiDeployment

  MetadataFunctionLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      LogGroupName: !Sub "/aws/lambda/${MetadataFunction}"
      RetentionInDays: 14

  LambdaSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: "Lambda Security Group"
      VpcId: !ImportValue { Fn::Sub: "${NetworkStackName}-VpcId" }

Outputs:
  ApiEndpoint:
    Description: "URL of the API endpoint"
    Value: !Sub "https://${MyApi}.execute-api.${AWS::Region}.amazonaws.com/Prod/images"
  MetadataFunctionName:
    Value: !Ref MetadataFunction
    Export: { Name: !Sub "${AWS::StackName}-FunctionName" }
  ImageEventsDLQName:
    Description: "Name of the SQS Dead Letter Queue"
    Value: !GetAtt ImageEventsDLQ.QueueName
    Export: { Name: !Sub "${AWS::StackName}-ImageEventsDLQName" }
```

#### 2.2. `monitoring-stack.yml`

**Назначение:** Наша "сигнализация".

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Transform: AWS::Serverless-2016-10-31
Description: 'Monitoring Stack: CloudWatch Alarms for Lambda errors and DLQ messages.'

Parameters:
  AppStackName:
    Type: String
    Description: "Name of the application stack (e.g., my-app-application)."
  AlarmEmail:
    Type: String
    Description: "Email to send alarm notifications to."

Resources:
  # SNS Топик, куда будут падать все алерты
  AlarmSnsTopic:
    Type: AWS::SNS::Topic
    Properties:
      Subscription:
        - Endpoint: !Ref AlarmEmail
          Protocol: "email"

  # Аларм на ошибки лямбды
  LambdaErrorAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmDescription: "Alarm if the Lambda function has errors"
      Namespace: "AWS/Lambda"
      MetricName: "Errors"
      Dimensions:
        - Name: "FunctionName"
          Value: !ImportValue { Fn::Sub: "${AppStackName}-FunctionName" }
      Statistic: "Sum"
      Period: 60
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: "GreaterThanOrEqualToThreshold"
      AlarmActions: [ !Ref AlarmSnsTopic ]

  # САМЫЙ ВАЖНЫЙ АЛАРМ: если в DLQ появилось хоть одно сообщение
  DLQNotEmptyAlarm:
    Type: AWS::CloudWatch::Alarm
    Properties:
      AlarmDescription: "Alarm if the DLQ is not empty"
      Namespace: "AWS/SQS"
      MetricName: "ApproximateNumberOfMessagesVisible"
      Dimensions:
        - Name: "QueueName"
          Value: !GetAtt # Логическое имя DLQ в application-stack.yml
            - my-app-application # Имя стека приложения
            - Outputs.ImageEventsDLQName # Нужно добавить этот Output в application-stack
      Statistic: "Sum"
      Period: 60
      EvaluationPeriods: 1
      Threshold: 1
      ComparisonOperator: "GreaterThanOrEqualToThreshold"
      AlarmActions: [ !Ref AlarmSnsTopic ]
```
**Важно:** Чтобы `DLQNotEmptyAlarm` заработал, добавь в `application-stack.yml` в секцию `Outputs` вот это:
```yaml
  ImageEventsDLQName:
    Description: "Name of the SQS Dead Letter Queue"
    Value: !GetAtt ImageEventsDLQ.QueueName
    Export: { Name: !Sub "${AWS::StackName}-ImageEventsDLQName" }
```

---

### Часть 3: Фабрика (CI/CD на GitHub Actions)

Ручной деплой — для слабаков. Выкидываем AWS CodePipeline и делаем всё на GitHub Actions, чтобы CI/CD жил рядом с кодом. Для аутентификации используем OpenID Connect (OIDC) — это самый безопасный способ, не требующий хранения долгоживущих ключей в секретах GitHub.

#### 3.1. IAM-роль для GitHub Actions

Сначала нужно создать в AWS специальную роль, которую GitHub сможет "надевать" во время выполнения workflow.

**Инструкция:**
1.  Создай в корне проекта файл `iam-github-role.yml`.
2.  Скопируй в него содержимое ниже.
3.  Разверни его ОДИН РАЗ командой, указанной после блока кода.

**Содержимое `iam-github-role.yml`:**
```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: 'IAM Role for GitHub Actions (Temporary Admin Version for Debugging)'

Parameters:
  GitHubOrg:
    Type: String
    Description: "Your GitHub organization or username."
  GitHubRepo:
    Type: String
    Description: "Your GitHub repository name."

Resources:
  # OIDC провайдер для GitHub. Создается один раз на аккаунт.
  GitHubOIDCProvider:
    Type: AWS::IAM::OIDCProvider
    Properties:
      Url: https://token.actions.githubusercontent.com
      ClientIdList:
        - sts.amazonaws.com
      ThumbprintList:
        - 6938fd4d98bab03faadb97b34396831e3780aea1

  # Сама роль, теперь с админскими правами
  GitHubActionsRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: !Sub 'GitHubActionsRole-${GitHubRepo}'
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Federated: !Ref GitHubOIDCProvider
            Action: sts:AssumeRoleWithWebIdentity
            Condition:
              StringLike:
                "token.actions.githubusercontent.com:sub": !Sub "repo:${GitHubOrg}/${GitHubRepo}:ref:refs/heads/main"
      # ВНИМАНИЕ: ВРЕМЕННОЕ РЕШЕНИЕ! ИСПОЛЬЗУЕМ ГОТОВУЮ ПОЛИТИКУ АДМИНИСТРАТОРА.
      # ЭТО НЕБЕЗОПАСНО ДЛЯ ПРОДА, НО ПОМОЖЕТ НАМ ИЗОЛИРОВАТЬ ПРОБЛЕМУ.
      ManagedPolicyArns:
        - 'arn:aws:iam::aws:policy/AdministratorAccess'

Outputs:
  GitHubActionsRoleArn:
    Description: "ARN of the IAM role for GitHub Actions"
    Value: !GetAtt GitHubActionsRole.Arn
```

**Команда для развертывания IAM-роли:**
```bash
aws cloudformation deploy \
  --template-file iam-github-role.yml \
  --stack-name my-app-github-role \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubOrg=YourGitHubUsername \
    GitHubRepo=YourRepoName
```
> Замени `YourGitHubUsername` и `YourRepoName` на свои. После выполнения скопируй `GitHubActionsRoleArn` из вывода команды. Он понадобится для workflow.

#### 3.2. Workflow для GitHub Actions

**Инструкция:**
1.  В корне своего репозитория создай папку `.github`, а в ней — папку `workflows`.
2.  Внутри `.github/workflows/` создай файл `deploy.yml`.
3.  Скопируй в него содержимое ниже.

**Содержимое `.github/workflows/deploy.yml`:**
```yaml
name: Deploy SAM Application

on:
  push:
    branches:
      - main # Запускать при пуше в ветку main

jobs:
  deploy:
    runs-on: ubuntu-latest
    permissions:
      id-token: write # Нужно для OIDC аутентификации
      contents: read  # Нужно для checkout

    steps:
      - name: Checkout repository
        uses: actions/checkout@v3

      - name: Setup Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.9'

      - name: Setup SAM CLI
        uses: aws-actions/setup-sam@v2

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v2
        with:
          # Вставь сюда ARN роли, который ты получил на предыдущем шаге
          role-to-assume: arn:aws:iam::ACCOUNT_ID:role/GitHubActionsRole-YourRepoName
          aws-region: eu-central-1 # Укажи свой регион

      - name: SAM Build
        run: sam build --use-container

      - name: SAM Deploy
        run: |
          sam deploy \
            --no-confirm-changeset \
            --no-fail-on-empty-changeset \
            --stack-name my-app-application \
            --s3-bucket aws-sam-cli-managed-default-samclisourcebucket-xxxxxxxx \
            --capabilities CAPABILITY_IAM \
            --parameter-overrides \
              NetworkStackName=my-app-network \
              StorageStackName=my-app-storage
```
**Важно:**
-   Замени `role-to-assume` на ARN, который ты получил.
-   Замени `aws-region` на свой регион.
-   Замени `s3-bucket` на имя бакета, который SAM использует для артефактов. Обычно он создается автоматически при первом ручном `sam deploy` и имеет вид `aws-sam-cli-managed-default-samclisourcebucket-<RANDOM_STRING>`. Ты можешь найти его в консоли S3.

Теперь любой `git push` в `main` будет запускать этот workflow и автоматически деплоить твое приложение.

---

### Часть 4: Порядок Развертывания и Очистка

#### Развертывание

1.  **Сеть (один раз):**
    ```bash
    aws cloudformation deploy --template-file network-stack.yml --stack-name my-app-network
    ```
2.  **Хранилище (один раз):**
    ```bash
    aws cloudformation deploy --template-file storage-stack.yml --stack-name my-app-storage --capabilities CAPABILITY_NAMED_IAM
    ```
3.  **IAM-роль для CI/CD (один раз):**
    ```bash
    aws cloudformation deploy \
      --template-file iam-github-role.yml \
      --stack-name my-app-github-role \
      --capabilities CAPABILITY_NAMED_IAM \
      --parameter-overrides \
        GitHubOrg=YourGitHubUsername \
        GitHubRepo=YourRepoName
    ```
4.  **Приложение (первый раз — в режиме гида):**
    > Для первого развертывания используем флаг `--guided`. SAM задаст несколько вопросов (имя стека, регион и т.д.), создаст S3-бакет для артефактов и сгенерирует файл `samconfig.toml`, который упростит все последующие деплои.
    ```bash
    sam deploy --guided --template-file application-stack.yml
    ```
5.  **Мониторинг (один раз):**
    ```bash
    aws cloudformation deploy --template-file monitoring-stack.yml --stack-name my-app-monitoring --parameter-overrides AppStackName=my-app-application AlarmEmail=your-email@example.com
    ```
    > Не забудь подтвердить подписку в письме от AWS!

После этого настрой `deploy.yml` и пуш в `main`. Дальнейшие деплои пойдут автоматически.

#### Полная очистка

Удаляй в **обратном порядке**:
```bash
aws cloudformation delete-stack --stack-name my-app-monitoring
aws cloudformation delete-stack --stack-name my-app-application
aws cloudformation delete-stack --stack-name my-app-github-role
aws cloudformation delete-stack --stack-name my-app-storage
aws cloudformation delete-stack --stack-name my-app-network
```

---

### Хвост от старика

1.  **NAT Gateway — это дорого.** Он стоит ~$35/месяц + плата за трафик. Если лямбде не нужен доступ во внешний интернет (только к сервисам AWS), смело удаляй `NatGateway`, `NatGatewayEIP` и `PrivateRoute` из `network-stack.yml`. Экономия налицо.
2.  **DLQ — не помойка.** Это реанимация. Сообщения в ней надо разбирать. Настрой вторую лямбду, которая по расписанию будет пытаться их обработать заново или хотя бы слать адекватный алерт в Slack с содержимым сбойного сообщения.
3.  **Холодные старты в VPC.** Раньше лямбды в VPC стартовали мучительно долго. Сейчас AWS это сильно улучшила, но проблема не исчезла до конца. Если у тебя критичный к задержкам API, используй `Provisioned Concurrency`, чтобы держать несколько экземпляров лямбды всегда "тёплыми". Это тоже стоит денег.
4.  **Секреты.** Мы не положили в код ни одного секрета, и это правильно. Для паролей от баз данных, API-ключей используй **AWS Secrets Manager**. Лямбде даются IAM-права на чтение конкретного секрета, а в коде ты его получаешь через AWS SDK.
5.  **GitHub Actions vs CodePipeline.** GitHub Actions удобнее, так как CI/CD-конфигурация живёт вместе с кодом. CodePipeline — нативный сервис AWS, его плюсы в более тесной интеграции с другими сервисами AWS (например, для сложных сценариев с ручным подтверждением) и в том, что вся инфраструктура описывается в одном стиле (CloudFormation). Выбор за тобой, но для большинства проектов GitHub Actions — то, что надо.
