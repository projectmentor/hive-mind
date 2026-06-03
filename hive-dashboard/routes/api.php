<?php

use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use App\Http\Controllers\Api\SyncController;

/*
|--------------------------------------------------------------------------
| API Routes
|--------------------------------------------------------------------------
|
| Here is where you can register API routes for your application. These
| routes are loaded by the RouteServiceProvider and all of them will
| be assigned to the "api" middleware group. Make something great!
|
*/

Route::middleware('api')->group(function () {
    Route::prefix('sync')->group(function () {
        Route::post('/receive', [SyncController::class, 'receive']);
        Route::get('/entries', [SyncController::class, 'entries']);
        Route::get('/facts', [SyncController::class, 'facts']);
    });
});