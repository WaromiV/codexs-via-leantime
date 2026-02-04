FROM leantime/leantime:latest

USER root
RUN apk add --no-cache git unzip curl
RUN curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

WORKDIR /var/www/html
RUN COMPOSER_ALLOW_SUPERUSER=1 composer require react/socket:^1.15 --no-interaction --prefer-dist

USER www-data
